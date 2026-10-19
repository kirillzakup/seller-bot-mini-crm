"""AI manager tests with a mocked LLM (no real API calls)."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC

from sqlalchemy import func, select

import app.sales.ai_manager as ai
from app.db.base import AsyncSessionLocal
from app.db.models import Order, PaymentMethod, Product, User
from app.db.models.enums import Channel, FulfillmentType, PaymentProviderType
from app.services.settings_service import set_setting


class FakeMessenger:
    def __init__(self) -> None:
        self.channel = Channel.TELEGRAM
        self.sent: list[tuple[str, str]] = []
        self.typing: list[tuple[str, str | None]] = []
        self.images: list[tuple[str, str, str]] = []
        self.media: list[tuple] = []

    async def send_text(self, external_id: str, text: str) -> None:
        self.sent.append((external_id, text))

    async def send_typing(self, external_id: str, *, message_id: str | None = None) -> None:
        self.typing.append((external_id, message_id))

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        self.images.append((external_id, url, caption))

    async def send_media(self, external_id, *, url, mime, kind, filename="", caption=""):
        self.media.append((external_id, url, kind, filename, caption))
        return "mid_doc"


async def _seed(session) -> tuple[User, Product]:
    n = random.randint(1, 10**9)
    user = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Тест")
    product = Product(
        sku=f"ai{n}",
        title="Онлайн-курс",
        price_kzt=9000,
        fulfillment_type=FulfillmentType.TEXT_LINK,
        fulfillment_config={"text": "Доступ к курсу"},
    )
    method = PaymentMethod(type=PaymentProviderType.MANUAL, title="Kaspi", instructions="реквизиты")
    session.add_all([user, product, method])
    await session.flush()
    await set_setting(
        session,
        "llm",
        {"base_url": "http://fake/v1", "api_key": "k", "model": "m", "temperature": 0.2},
    )
    return user, product


def test_ai_creates_order_via_tool(monkeypatch):
    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _seed(s)

            calls = {"n": 0}

            async def fake_cc(*, messages, tools, config):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "create_payment",
                                    "arguments": json.dumps({"product_id": product.id}),
                                },
                            }
                        ],
                    }
                return {"role": "assistant", "content": "Заказ оформлен, вот ссылка на оплату."}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)

            msg = FakeMessenger()
            await ai.handle_text(s, user=user, text="хочу купить курс", messenger=msg, tg_bot=None)
            await s.commit()

            count = (
                await s.execute(select(func.count(Order.id)).where(Order.user_id == user.id))
            ).scalar_one()
            assert count == 1
            assert msg.sent and "ссылк" in msg.sent[-1][1].lower()

    asyncio.run(main())


def test_ai_fallback_when_not_configured(monkeypatch):
    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            # Empty LLM config → handle_text must reply gracefully, not crash
            await set_setting(s, "llm", {"base_url": "", "api_key": "", "model": "m"})

            msg = FakeMessenger()
            await ai.handle_text(s, user=user, text="привет", messenger=msg, tg_bot=None)
            await s.commit()

            assert msg.sent  # a fallback message was sent
            assert "оператор" in msg.sent[-1][1].lower()

    asyncio.run(main())


def test_start_message_ai_generated(monkeypatch):
    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            await set_setting(s, "welcome_text", "Представься как Захра и предложи помощь")

            captured = {}

            async def fake_cc(*, messages, tools, config):
                captured["messages"] = messages
                return {"role": "assistant", "content": "Здравствуйте! Меня зовут Захра 🙂"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)

            greeting = await ai.generate_start_message(s, user=user)
            assert greeting == "Здравствуйте! Меня зовут Захра 🙂"
            # The admin welcome text was used as the trigger, not sent verbatim
            assert any("Захра" in m.get("content", "") for m in captured["messages"])

    asyncio.run(main())


def test_start_message_fallback_when_llm_off(monkeypatch):
    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            await set_setting(s, "welcome_text", "Добро пожаловать в магазин")

            async def fake_cc(*, messages, tools, config):
                from app.sales.llm import LLMNotConfigured

                raise LLMNotConfigured("off")

            monkeypatch.setattr(ai, "chat_completion", fake_cc)

            greeting = await ai.generate_start_message(s, user=user)
            assert greeting == "Добро пожаловать в магазин"  # verbatim fallback

    asyncio.run(main())


def test_bot_paused_skips_ai_but_records(monkeypatch):
    """When a chat is paused (operator took over), the AI stays silent but the
    client's incoming message is still recorded for the inbox."""
    import app.channels.dispatch as disp
    from app.channels.base import IncomingMessage
    from app.db.models import Message

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            user.bot_paused = True
            await s.flush()

            called = {"n": 0}

            async def fake_handle(*a, **k):
                called["n"] += 1

            monkeypatch.setattr(disp.ai_manager, "handle_text", fake_handle)

            msg = IncomingMessage(
                channel=user.channel, external_id=user.external_id, text="сообщение на паузе"
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()

            assert called["n"] == 0  # bot did NOT reply
            rows = (
                (await s.execute(select(Message).where(Message.user_id == user.id)))
                .scalars()
                .all()
            )
            assert any("сообщение на паузе" in m.content for m in rows)  # but it's recorded

    asyncio.run(main())


def test_inbound_voice_recorded_even_when_transcription_off(monkeypatch):
    """A client's voice note is saved with a playable audio URL even when voice
    transcription is disabled — so it stays visible in «Диалоги» and the operator can
    listen (regression: voice messages vanished entirely when STT was off)."""
    import app.channels.dispatch as disp
    from app.channels.base import IncomingMessage
    from app.db.models import Message

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)  # voice disabled by default

            class VoiceMessenger(FakeMessenger):
                async def download_audio(self, ref):
                    return b"OggS-FAKEVOICE"

            fake = VoiceMessenger()

            async def fake_get_messenger(sess, channel):
                return fake

            saved = {"url": "https://x/media/v.ogg", "kind": "voice"}
            monkeypatch.setattr(disp, "get_messenger", fake_get_messenger)
            monkeypatch.setattr(disp, "save_inbound_audio", lambda data: saved)

            msg = IncomingMessage(
                channel=user.channel,
                external_id=user.external_id,
                attachments=[{"kind": "audio", "file_id": "MID"}],
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()

            row = (
                await s.execute(select(Message).where(Message.user_id == user.id))
            ).scalars().first()
            assert row is not None  # the voice note is recorded, not dropped
            assert row.media_kind == "voice"
            assert row.media_url == "https://x/media/v.ogg"
            assert row.content == "🎤 Голосовое"

    asyncio.run(main())


def test_inbound_voice_attaches_audio_to_transcribed_turn(monkeypatch):
    """When transcription is on, the user turn keeps BOTH the transcribed text and the
    playable audio (media_url/kind='voice'), so operators can read and replay it."""
    import app.channels.dispatch as disp
    from app.channels.base import IncomingMessage

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            await set_setting(
                s, "voice", {"enabled": True, "api_key": "k", "voice_id": "v", "reply_mode": "off"}
            )

            class VoiceMessenger(FakeMessenger):
                async def download_audio(self, ref):
                    return b"OggS-FAKEVOICE"

            fake = VoiceMessenger()

            async def fake_get_messenger(sess, channel):
                return fake

            async def fake_transcribe(audio, *, filename, cfg):
                return "почём ваш товар"

            captured = {}

            async def fake_handle(session, *, user, text, messenger, **k):
                captured.update(
                    text=text, media_url=k.get("media_url"), media_kind=k.get("media_kind")
                )

            saved = {"url": "https://x/media/v.ogg", "kind": "voice"}
            monkeypatch.setattr(disp, "get_messenger", fake_get_messenger)
            monkeypatch.setattr(disp, "save_inbound_audio", lambda data: saved)
            monkeypatch.setattr(disp.voice, "transcribe", fake_transcribe)
            monkeypatch.setattr(disp.ai_manager, "handle_text", fake_handle)

            msg = IncomingMessage(
                channel=user.channel,
                external_id=user.external_id,
                attachments=[{"kind": "audio", "file_id": "MID"}],
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()

            assert captured["text"] == "почём ваш товар"
            assert captured["media_url"] == "https://x/media/v.ogg"
            assert captured["media_kind"] == "voice"

    asyncio.run(main())


def test_bot_voice_reply_attaches_playable_audio(monkeypatch):
    """When the bot answers with voice, the saved assistant turn keeps the text AND a
    playable audio URL (media_kind='voice'), so operators can replay it in «Диалоги»."""
    from app.db.models import Message
    from app.db.models.enums import ConversationRole
    from app.voice.elevenlabs import VoiceReply

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            vcfg = {"enabled": True, "api_key": "k", "voice_id": "v", "reply_mode": "always"}
            await set_setting(s, "voice", vcfg)

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Здравствуйте! Кирпич есть в наличии."}

            async def fake_synth(text, *, cfg, instagram=False):
                return VoiceReply(data=b"OggS-BOTVOICE", mime="audio/ogg", filename="reply.ogg")

            saved = {"url": "https://x/media/b.ogg", "kind": "voice"}

            class VoiceMessenger(FakeMessenger):
                async def send_voice(self, external_id, audio, *, mime, filename):
                    return True

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            monkeypatch.setattr(ai.voice, "synthesize_reply", fake_synth)
            monkeypatch.setattr(ai, "save_inbound_audio", lambda data: saved)

            await ai.handle_text(
                s, user=user, text="есть в наличии?", messenger=VoiceMessenger(), reply_voice=True
            )
            await s.commit()

            row = (
                await s.execute(
                    select(Message).where(
                        Message.user_id == user.id, Message.role == ConversationRole.ASSISTANT
                    )
                )
            ).scalars().first()
            assert row is not None
            assert row.media_kind == "voice"
            assert row.media_url == "https://x/media/b.ogg"
            assert "наличии" in row.content  # text kept alongside the audio

    asyncio.run(main())


def test_request_callback_saves_phone():
    """Callback request saves the client's phone/name and hands off to the manager."""
    from app.sales.tools import run_tool

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            res = await run_tool(
                "request_callback",
                {"name": "Иван", "phone": "+77001234567", "note": "хочет курс"},
                session=s,
                user=user,
                messenger=FakeMessenger(),
                tg_bot=None,
            )
            await s.commit()
            assert user.phone == "+77001234567"
            assert user.first_name == "Иван"
            assert user.name_confirmed is True  # client gave their name → bot may use it
            assert "менеджер" in res.lower()

    asyncio.run(main())


def test_resend_access_redelivers_paid_course():
    """A paid client asking for their link gets the REAL course link re-sent — never a
    made-up 'find it on the site' link (regression: bot hallucinated example.kz)."""
    from app.db.models.enums import OrderStatus
    from app.sales.tools import run_tool

    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _seed(s)  # product: text_link, cfg text "Доступ к курсу"
            order = Order(
                code=f"RA{random.randint(1, 10**6)}",
                user_id=user.id,
                product_id=product.id,
                amount_kzt=product.price_kzt,
                status=OrderStatus.PAID,
            )
            s.add(order)
            await s.flush()

            msg = FakeMessenger()
            res = await run_tool(
                "resend_access", {}, session=s, user=user, messenger=msg, tg_bot=None
            )
            await s.commit()
            assert msg.sent  # the real access was re-sent to the client…
            assert "Доступ к курсу" in msg.sent[-1][1]  # …the fulfillment text from the card
            assert "переотправил" in res.lower()

    asyncio.run(main())


def test_resend_access_without_paid_orders_stays_honest():
    """No paid orders → the tool tells the bot NOT to invent a link or point to a site."""
    from app.sales.tools import run_tool

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            res = await run_tool(
                "resend_access", {}, session=s, user=user, messenger=FakeMessenger(), tg_bot=None
            )
            low = res.lower()
            assert "нет оплаченных" in low
            assert "сайт" in low  # explicit: do not send to a site / invent links

    asyncio.run(main())


def test_system_prompt_forbids_stalling_and_offers_resend():
    """The prompt steers the bot to act in-turn (no 'please wait') and to re-send real
    access instead of inventing a site link (regression: bot stalled with «подождите»)."""

    async def main():
        async with AsyncSessionLocal() as s:
            await _seed(s)
            prompt = (await ai.build_system_prompt(s)).lower()
            assert "resend_access" in prompt  # the re-send-access tool is offered
            assert "подожд" in prompt  # anti-stall rule («не пиши подождите»)
            assert "сайт" in prompt  # no-site / no-invented-link rule

    asyncio.run(main())


def test_is_access_stall_catches_hallucinated_promises():
    from app.sales.ai_manager import _is_access_stall

    # real hallucinations seen in production always carry an access/payment claim
    assert _is_access_stall("Оплата подтверждена! Переотправлю вам доступ.")
    assert _is_access_stall("Оплата прошла! Отправлю вам доступ, дайте секунду.")
    assert _is_access_stall("Дам вам ссылку на курс через минуту.")
    assert _is_access_stall("Ваш заказ успешно выполнен 🎉")
    # ordinary stalls WITHOUT an access/payment claim must NOT trigger the override
    assert not _is_access_stall("Здравствуйте! Какой курс вас интересует?")
    assert not _is_access_stall("Секундочку, уточню расписание занятий.")


def test_stall_without_payment_gets_honest_override(monkeypatch):
    """Model stalls/claims payment but there's no paid order → the client gets an honest
    'no payment found', NOT the false 'order confirmed'."""

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)  # no orders

            async def fake_cc(*, messages, tools, config):
                return {
                    "role": "assistant",
                    "content": "Ваш заказ подтверждён! Дайте мне секунду, отправлю доступ.",
                }

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            msg = FakeMessenger()
            await ai.handle_text(
                s, user=user, text="скинь ссылку на курс", messenger=msg, tg_bot=None
            )
            await s.commit()
            assert msg.sent
            last = msg.sent[-1][1].lower()
            assert "не вижу" in last  # honest: no paid order
            assert "подтвержд" not in last  # the false "order confirmed" was suppressed

    asyncio.run(main())


def test_stall_with_paid_order_delivers_real_access(monkeypatch):
    """Model stalls but there IS a paid order → the real course link is delivered, not
    the stall text."""
    from app.db.models.enums import OrderStatus

    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _seed(s)  # product: text_link, cfg text "Доступ к курсу"
            order = Order(
                code=f"SN{random.randint(1, 10**6)}",
                user_id=user.id,
                product_id=product.id,
                amount_kzt=product.price_kzt,
                status=OrderStatus.PAID,
            )
            s.add(order)
            await s.flush()

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Дайте мне секунду, отправлю доступ 🙂"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            msg = FakeMessenger()
            await ai.handle_text(
                s, user=user, text="скинь ссылку на курс", messenger=msg, tg_bot=None
            )
            await s.commit()
            assert any("Доступ к курсу" in t for _, t in msg.sent)  # real link delivered…
            assert not any("секунду" in t.lower() for _, t in msg.sent)  # …stall suppressed

    asyncio.run(main())


def test_send_push_no_subscriptions_is_noop():
    """With no operator devices subscribed, send_push does nothing (0 sent, no network)."""
    from app.services.push import send_push

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "push_subscriptions", [])
            n = await send_push(s, "Тема", "Текст", url="/admin/dialogs?u=1")
            assert n == 0

    asyncio.run(main())


def test_operator_role_maps_to_assistant_in_history():
    """Operator turns must NOT reach OpenAI as role='operator' — it 400s the request.

    Regression: a human operator reply (Диалоги takeover) is stored role=operator;
    when the bot resumes and reloads history, that role must be remapped."""
    from app.sales.conversation import load_history, save_message

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            await save_message(s, user.id, "user", "Здравствуйте")
            await save_message(s, user.id, "operator", "Это менеджер, подскажу по курсам")
            await save_message(s, user.id, "assistant", "Чем помочь?")
            hist = await load_history(s, user.id)
            roles = {h["role"] for h in hist}
            assert "operator" not in roles
            assert roles <= {"user", "assistant", "system", "tool"}
            assert any(
                h["content"] == "Это менеджер, подскажу по курсам" and h["role"] == "assistant"
                for h in hist
            )

    asyncio.run(main())


def test_typing_delay_scales_and_clamps():
    """The human-pacing delay scales with length and clamps to [min, max]; off → 0."""
    from app.sales.ai_manager import _DEFAULT_HUMANIZE, _typing_delay

    cfg = _DEFAULT_HUMANIZE
    assert _typing_delay(3, cfg) == cfg["min_seconds"]  # tiny reply → floor
    assert _typing_delay(100_000, cfg) == cfg["max_seconds"]  # huge reply → cap
    assert _typing_delay(500, {"enabled": False}) == 0.0  # disabled → no pause
    mid = _typing_delay(80, cfg)
    assert cfg["min_seconds"] <= mid <= cfg["max_seconds"]


def test_typing_indicator_shown_before_reply(monkeypatch):
    """The bot shows a 'typing…' indicator before its reply (all channels)."""

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Здравствуйте! Чем помочь?"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            msg = FakeMessenger()
            await ai.handle_text(s, user=user, text="привет", messenger=msg, tg_bot=None)
            await s.commit()

            assert msg.typing  # typing indicator was shown…
            assert msg.sent  # …before the actual reply landed

    asyncio.run(main())


def test_conversation_memory_persists(monkeypatch):
    """The bot reloads prior turns from the DB before answering (memory across turns)."""

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            captured = {}

            async def fake_cc(*, messages, tools, config):
                captured["messages"] = messages
                return {"role": "assistant", "content": "Отвечаю: ЗАПОМНИЛ"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)

            msg = FakeMessenger()
            await ai.handle_text(s, user=user, text="ПЕРВЫЙ_ВОПРОС", messenger=msg, tg_bot=None)
            await ai.handle_text(s, user=user, text="ВТОРОЙ_ВОПРОС", messenger=msg, tg_bot=None)
            await s.commit()

            # On the 2nd call the history must carry the 1st Q + its reply + the new Q.
            contents = [m.get("content", "") for m in captured["messages"]]
            assert any("ПЕРВЫЙ_ВОПРОС" in c for c in contents)
            assert any("ЗАПОМНИЛ" in c for c in contents)
            assert any("ВТОРОЙ_ВОПРОС" in c for c in contents)

    asyncio.run(main())


def test_schedule_follow_up_creates_row():
    """The AI tool persists a pending follow-up for the lead."""
    from app.db.models import Followup
    from app.sales.tools import run_tool

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            res = await run_tool(
                "schedule_follow_up",
                {"message": "Определились по курсу?", "hours": 5},
                session=s,
                user=user,
                messenger=FakeMessenger(),
                tg_bot=None,
            )
            await s.commit()
            rows = (
                (await s.execute(select(Followup).where(Followup.user_id == user.id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].message == "Определились по курсу?"
            assert rows[0].sent is False
            assert "напомн" in res.lower()

    asyncio.run(main())


def test_schedule_follow_up_exact_time():
    """A client-named exact local time is stored as the correct UTC due time."""
    from datetime import datetime, timedelta, timezone

    from app.db.models import Followup
    from app.sales.tools import run_tool

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            local = datetime.now(timezone(timedelta(hours=5))) + timedelta(days=2)
            at = local.strftime("%Y-%m-%dT%H:%M:%S")  # naive local ISO
            await run_tool(
                "schedule_follow_up",
                {"message": "перезвоним", "at": at},
                session=s,
                user=user,
                messenger=FakeMessenger(),
                tg_bot=None,
            )
            await s.commit()
            fu = (
                (
                    await s.execute(
                        select(Followup)
                        .where(Followup.user_id == user.id)
                        .order_by(Followup.id.desc())
                    )
                )
                .scalars()
                .first()
            )
            assert fu is not None
            expected = local.astimezone(UTC)
            due = fu.due_at if fu.due_at.tzinfo else fu.due_at.replace(tzinfo=UTC)
            assert abs((due - expected).total_seconds()) < 120

    asyncio.run(main())


def test_system_prompt_includes_script_and_downsell():
    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _seed(s)
            await set_setting(s, "sales_script", "УНИКАЛЬНЫЙ_МАРКЕР_СКРИПТА: установить контакт")
            await set_setting(s, "downsell", {"enabled": True, "product_id": product.id})
            prompt = await ai.build_system_prompt(s)
            assert "УНИКАЛЬНЫЙ_МАРКЕР_СКРИПТА" in prompt
            assert "Даунселл" in prompt and product.title in prompt
            assert "schedule_follow_up" in prompt  # follow-up guidance present
            await set_setting(s, "downsell", {"enabled": False, "product_id": None})

    asyncio.run(main())


def test_escalate_without_operator_chat_is_honest():
    """No operator chat configured → escalate must NOT claim the operator was notified."""
    from app.sales.tools import run_tool

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            await set_setting(s, "operator_chat_id", None)
            await set_setting(s, "admin_chat_id", None)
            return await run_tool(
                "escalate_to_operator",
                {"reason": "клиент просит человека"},
                session=s,
                user=user,
                messenger=FakeMessenger(),
                tg_bot=None,
            )

    result = asyncio.run(main())
    low = result.lower()
    assert "не настроен" in low or "не доставлено" in low
    assert "уведомлён" not in low or "не" in low  # never a bare "оператор уведомлён"


def test_style_format_and_count():
    """Owner vs client labelling and owner-message counting for style learning."""
    from app.sales.style import _format_dialogs, count_owner_messages

    dialogs = [
        {
            "messages": [
                {"from": "shop_owner", "text": "Здравствуйте! 🙂"},
                {"from": "client1", "text": "сколько стоит?"},
                {"from": "Shop_Owner", "text": "Комплект 50000 тг"},  # case-insensitive
                {"from": "shop_owner", "text": ""},  # empty → ignored
            ]
        }
    ]
    assert count_owner_messages(dialogs, "shop_owner") == 2
    out = _format_dialogs(dialogs, "shop_owner")
    assert "[МЕНЕДЖЕР] Здравствуйте! 🙂" in out
    assert "[КЛИЕНТ] сколько стоит?" in out
    assert "[МЕНЕДЖЕР] Комплект 50000 тг" in out


def test_build_system_prompt_includes_style():
    """An applied style_guide is injected into the system prompt as a distinct block."""

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "style_guide", "Пиши тепло, на «Вы», эмодзи 🤗")
            prompt = await ai.build_system_prompt(s)
            await set_setting(s, "style_guide", "")  # reset for other tests
            return prompt

    prompt = asyncio.run(main())
    assert "МАНЕРА ОБЩЕНИЯ" in prompt
    assert "эмодзи 🤗" in prompt


def test_build_system_prompt_pins_gender():
    """A hard identity anchor pins the bot's Russian gendered forms so it stops
    flipping between «рад» and «рада». Default feminine; 'male' switches the forms."""

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "bot_name", "Анна")
            await set_setting(s, "bot_gender", "female")
            female = await ai.build_system_prompt(s)
            await set_setting(s, "bot_gender", "male")
            male = await ai.build_system_prompt(s)
            await set_setting(s, "bot_name", "")  # reset for other tests
            await set_setting(s, "bot_gender", "female")
            return female, male

    female, male = asyncio.run(main())
    assert "ЛИЧНОСТЬ" in female and "Анна" in female
    assert "женщина" in female and "«рада»" in female and "«рад»" not in female
    assert "мужчина" in male and "«рад»" in male


def test_media_block_surfaces_tags_as_trigger():
    """The media block must show each image's tags (повод) so the bot knows WHEN to
    send it — not just the caption — and instruct it to call send_image by повод."""
    from app.db.models import MediaAsset

    async def main():
        async with AsyncSessionLocal() as s:
            m = MediaAsset(
                title="Ждун ждёт ответа",
                filename="zhdun_ai_test.jpg",
                tags="тишина, игнор, подумаю",
                is_active=True,
            )
            s.add(m)
            await s.flush()
            prompt = await ai.build_system_prompt(s)
            await s.delete(m)  # keep the shared test DB clean
            await s.commit()
            return prompt

    prompt = asyncio.run(main())
    assert "send_image" in prompt
    assert "повод" in prompt  # tags surfaced as the trigger, not just the caption
    assert "подумаю" in prompt  # the actual tag word the bot passes as query


def test_reaction_meme_fires_on_tag_word(monkeypatch):
    """A client message containing a meme's tag word («позже») makes the bot attach that
    meme after its text reply — deterministically, not relying on the LLM to decide."""
    from app.db.models import MediaAsset

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            asset = MediaAsset(
                title="Ждун",
                filename="reaction_test.jpg",
                tags="подумаю, позже, потом",
                is_active=True,
            )
            s.add(asset)
            await s.flush()
            aid = asset.id

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Хорошо, буду ждать 🙂"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            msg = FakeMessenger()
            await ai.handle_text(s, user=user, text="давайте позже", messenger=msg, tg_bot=None)
            await s.delete(await s.get(MediaAsset, aid))  # keep the shared test DB clean
            await s.commit()
            return msg

    msg = asyncio.run(main())
    assert any("reaction_test.jpg" in url for _, url, _ in msg.images)  # meme was attached


def test_send_image_by_number_picks_from_gallery():
    """The model can send a picture by its list NUMBER (query='2') — resolved by position,
    not word-match — so its semantic/emotional choice reliably reaches the right image."""
    from app.db.models import MediaAsset
    from app.sales.tools import run_tool
    from app.services.media import active_media

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            joy = MediaAsset(
                title="Радость", filename="joy_test.jpg", tags="радость", is_active=True
            )
            sad = MediaAsset(
                title="Грусть", filename="sad_test.jpg", tags="грусть", is_active=True
            )
            s.add_all([joy, sad])
            await s.flush()
            ids = (joy.id, sad.id)
            gallery = await active_media(s)
            pos = next(i for i, m in enumerate(gallery, 1) if m.id == sad.id)  # 1-based number
            msg = FakeMessenger()
            res = await run_tool(
                "send_image", {"query": str(pos)}, session=s, user=user, messenger=msg, tg_bot=None
            )
            for i in ids:
                await s.delete(await s.get(MediaAsset, i))  # keep the shared test DB clean
            await s.commit()
            return res, msg

    res, msg = asyncio.run(main())
    assert any("sad_test.jpg" in url for _, url, _ in msg.images)  # the numbered pick resolved
    assert "Грусть" in res


def test_reaction_meme_fires_after_debounce(monkeypatch):
    """After a debounced burst (respond_from_history, new_text=None) the reaction meme
    still fires — it reads the client's last saved message, not new_text. Regression:
    memes never fired on WhatsApp/Instagram because those channels debounce."""
    from app.db.models import MediaAsset
    from app.sales.conversation import save_message

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            asset = MediaAsset(
                title="Ждун", filename="debounce_react.jpg", tags="подумаю, позже", is_active=True
            )
            s.add(asset)
            await s.flush()
            aid = asset.id
            await save_message(s, user.id, "user", "позже")  # fragment already saved by debounce

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Хорошо, жду 🙂"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            msg = FakeMessenger()
            await ai.respond_from_history(s, user=user, messenger=msg, tg_bot=None)
            await s.delete(await s.get(MediaAsset, aid))  # keep the shared test DB clean
            await s.commit()
            return msg

    msg = asyncio.run(main())
    assert any("debounce_react.jpg" in url for _, url, _ in msg.images)


def test_send_document_delivers_file():
    """send_document finds a document by words (title/tags/category) and sends it as a
    file via send_media (kind=document) — so the bot can share prices/catalogs/certs."""
    from app.sales.tools import run_tool
    from app.services.documents import delete_document, save_document

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _seed(s)
            doc = await save_document(
                s,
                title="Прайс на курсы 2026",
                data=b"%PDF-1.4 fake price",
                content_type="application/pdf",
                orig_name="price.pdf",
                category="прайс",
                tags="прайс, цены, стоимость",
            )
            assert doc is not None and doc.kind == "document"
            did = doc.id
            msg = FakeMessenger()
            res = await run_tool(
                "send_document",
                {"query": "прайс цены"},
                session=s,
                user=user,
                messenger=msg,
                tg_bot=None,
            )
            await delete_document(s, did)  # keep the shared test DB + disk clean
            await s.commit()
            return res, msg

    res, msg = asyncio.run(main())
    assert msg.media and msg.media[0][2] == "document"  # delivered as a file, not text
    assert "Прайс" in res


def test_debounce_collapses_burst_into_one_reply(monkeypatch):
    """A rapid burst of fragments schedules only ONE reply once the client goes quiet."""
    from app.sales import debounce

    calls = {"n": 0}

    async def fake_respond(session, *, user, messenger, tg_bot=None, incoming_message_id=None):
        calls["n"] += 1

    async def fake_get_messenger(session, channel):
        return object()

    monkeypatch.setattr(debounce.ai_manager, "respond_from_history", fake_respond)
    monkeypatch.setattr(debounce, "get_messenger", fake_get_messenger)

    async def main():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.INSTAGRAM, external_id="BURST-1")
            s.add(u)
            await s.commit()
            uid = u.id
        for _ in range(3):  # three fragments in quick succession
            debounce.schedule(uid, delay=0.05, channel=Channel.INSTAGRAM)
            await asyncio.sleep(0.01)  # each cancels the previous timer
        await asyncio.sleep(0.25)  # let the surviving timer fire
        return calls["n"]

    assert asyncio.run(main()) == 1  # one combined reply, not three


def test_receipts_status_ranking_and_cumulative_read():
    """apply_status never downgrades; mark_read_for_user marks ALL outbound read."""
    from app.db.models import Message
    from app.db.models.enums import ConversationRole
    from app.services import receipts

    async def main():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.WHATSAPP, external_id="77010009999")
            s.add(u)
            await s.flush()
            uid = u.id  # capture before expiring, so the later query needs no lazy IO
            m1 = Message(
                user_id=uid, role=ConversationRole.ASSISTANT,
                content="a", channel_mid="mid1", status="sent",
            )
            m2 = Message(
                user_id=uid, role=ConversationRole.OPERATOR,
                content="b", channel_mid="mid2", status="sent",
            )
            s.add_all([m1, m2])
            await s.flush()

            await receipts.apply_status(s, mid="mid1", status="delivered")
            assert m1.status == "delivered"
            await receipts.apply_status(s, mid="mid1", status="sent")  # never downgrades
            assert m1.status == "delivered"

            await receipts.apply_status(s, mid="mid2", status="failed")
            assert m2.status == "failed"

            await receipts.mark_read_for_user(s, uid)  # cumulative
            s.expire_all()
            rows = (
                await s.execute(select(Message).where(Message.user_id == uid))
            ).scalars().all()
            statuses = {r.status for r in rows}
            await s.rollback()
            return statuses

    # m1 (delivered) → read; m2 (failed) must NOT be repainted as read — a failed
    # send must stay visibly failed even after the client replies to something else.
    assert asyncio.run(main()) == {"read", "failed"}


def test_config_hardens_weak_secret_key():
    """A default/blank SECRET_KEY is swapped for a random ephemeral one; strong keys kept."""
    from app.config import Settings

    weak = Settings(secret_key="change-me-please", admin_password="admin")
    assert weak.secret_key != "change-me-please" and len(weak.secret_key) >= 32
    strong = "s" * 64
    assert Settings(secret_key=strong).secret_key == strong  # real secret untouched


def test_vapid_pem_is_loadable_for_push():
    """Regression: the stored VAPID private key MUST load via Vapid.from_pem — otherwise
    every web push silently fails to send (the bug where phones got no notifications)."""
    from py_vapid import Vapid01 as Vapid

    from app.services.push import get_vapid

    async def main():
        async with AsyncSessionLocal() as s:
            cfg = await get_vapid(s)
            Vapid.from_pem(cfg["private_pem"].encode())  # must not raise
            assert cfg["public_key"]

    asyncio.run(main())


def test_build_system_prompt_uses_name_only_if_client_confirmed():
    """A name is used only when the CLIENT confirmed it (name_confirmed). A platform handle
    (name_confirmed=False) must NOT be used — the bot is told to ask how to address the
    person and warned never to greet by the @handle/nick."""
    from app.db.models.enums import Channel
    from app.db.models.user import User

    async def main():
        async with AsyncSessionLocal() as s:
            confirmed = User(
                channel=Channel.INSTAGRAM, external_id="CTX1", first_name="Кирилл",
                name_confirmed=True,
            )
            handle = User(
                channel=Channel.INSTAGRAM, external_id="CTX2", first_name="xxx_killer_2007",
                name_confirmed=False,
            )
            return (
                await ai.build_system_prompt(s, confirmed),
                await ai.build_system_prompt(s, handle),
                await ai.build_system_prompt(s, None),
            )

    with_name, with_handle, anon = asyncio.run(main())
    assert "Кирилл" in with_name
    assert "НЕ спрашивай имя снова" in with_name
    assert "xxx_killer_2007" not in with_handle
    assert "как к клиенту можно обращаться" in with_handle
    assert "@username" in with_handle
    assert "как к клиенту можно обращаться" in anon
    assert "Кирилл" not in anon


# ── 04.08.2026: бот предлагал сначала дорогой 250×120×65 и КОЛОТЫЙ 250×60×65,
# хотя в приоритете гладкий 250×60×65 (отдельная матрица, без раскола).
# Каталог был отсортирован верно, но модель читала список как простой перечень.


def test_catalog_is_presented_as_priority_order():
    import asyncio

    from app.db.base import AsyncSessionLocal
    from app.sales.ai_manager import build_system_prompt

    async def run():
        async with AsyncSessionLocal() as s:
            return await build_system_prompt(s)

    prompt = asyncio.run(run())
    assert "В ПОРЯДКЕ" in prompt and "ПРИОРИТЕТА" in prompt, (
        "каталог должен подаваться как список приоритета, иначе модель предлагает что попало"
    )
    assert "начинай подбор с товаров в начале списка" in prompt
