"""Tools the AI manager can call (OpenAI function-calling) + their dispatch.

Tools are the safety boundary: the model never invents prices or grants access —
it calls these, which read/write the DB. Access delivery is intentionally NOT a
tool: it happens only via payment confirmation (server-gated in services.orders).
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Messenger
from app.config import get_settings
from app.db.models import Followup, Order, Product, User
from app.db.models.enums import Channel, OrderStatus
from app.fulfillment.registry import build_fulfillment
from app.knowledge.rag import active_products, search_kb
from app.sales.conversation import media_sent_recently, record_sent_media, record_sent_text
from app.services import leads_service
from app.services import orders as orders_svc
from app.services.documents import active_documents, search_documents
from app.services.media import active_media, public_url, search_media
from app.services.notify import client_block, send_operator_message
from app.services.settings_service import get_local_tz, get_setting

logger = logging.getLogger(__name__)


# ── Tool schemas (sent to the model) ─────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_products",
            "description": "Показать активные товары с ценами и id.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Получить подробности товара по его id.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_payment",
            "description": (
                "Оформить заказ и дать ссылку на оплату. Вызывай ТОЛЬКО после того, как "
                "клиент прямо согласился оформить заказ или сам попросил счёт. Расчёт "
                "по просьбе «посчитайте» заказом НЕ является. Обязательно передай "
                "quantity — количество штук из расчёта, иначе счёт выйдет на одну штуку."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "quantity": {
                        "type": "integer",
                        "description": "Количество в штуках, о котором договорились.",
                    },
                },
                "required": ["product_id", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_payment",
            "description": "Проверить статус последнего заказа клиента.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resend_access",
            "description": (
                "Переотправить клиенту доступ к УЖЕ ОПЛАЧЕННОМУ курсу — настоящую ссылку из "
                "карточки товара. Вызывай, когда оплативший клиент просит «скинь ссылку на "
                "курс», «где мой курс», не нашёл доступ. НИКОГДА не выдумывай ссылку и не "
                "отправляй на сайт."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Поиск в базе знаний: FAQ, возражения, инфо о продукте.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_operator",
            "description": "Позвать оператора, если клиент просит человека или ты не справляешься.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_admin",
            "description": (
                "Спросить администратора, когда клиент задал вопрос, ответа на который у тебя "
                "НЕТ (нет в каталоге/скрипте, и search_knowledge не помог). Передай ТОЧНЫЙ "
                "вопрос клиента — администратор ответит клиенту в его диалоге. Клиенту скажи, "
                "что уточняешь у специалиста и скоро вернёшься с ответом. НЕ выдумывай ответ."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Точный вопрос клиента"}
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_contact",
            "description": "Сохранить контактные данные клиента, если он их сообщил.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_callback",
            "description": (
                "Клиент просит перезвонить / хочет звонок вместо переписки. Передай заявку "
                "менеджеру. Если клиент хочет звонок на номер, с которого пишет (сказал «этот», "
                "«с этого», «тот же» и т.п.) или не диктует новый номер — НЕ указывай phone, "
                "система сама подставит его номер (для WhatsApp)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя клиента"},
                    "phone": {
                        "type": "string",
                        "description": (
                            "Номер для звонка, ТОЛЬКО если клиент продиктовал конкретный номер. "
                            "Если он имеет в виду номер, с которого пишет («этот») — оставь пустым."
                        ),
                    },
                    "note": {"type": "string", "description": "Кратко: по какому вопросу"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_image",
            "description": (
                "Отправить клиенту картинку/мем/пример из библиотеки. Укажи описание "
                "нужной картинки (например «мем про дедлайн», «пример сертификата», "
                "«фото офиса»). Используй уместно — оживить общение или показать пример."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Описание нужной картинки"},
                    "caption": {
                        "type": "string",
                        "description": "Короткая подпись к картинке (необязательно)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_asset",
            "description": (
                "Отправить клиенту материал из медиатеки по описанию/контексту: кружок "
                "(видеокружок), видео, готовое голосовое, ссылку или картинку. Пример "
                "query: «кружок с видом производства», «отзыв клиента», «ссылка на прайс». "
                "Бот сам подбирает подходящий материал."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Описание нужного материала",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Короткая подпись к материалу (необязательно)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_document",
            "description": (
                "Отправить клиенту документ из библиотеки: прайс, каталог, презентацию, "
                "сертификат или пример достижений. Укажи, что нужно (например «прайс на "
                "курсы», «каталог», «сертификат»). Отправляй, когда клиент просит цены или "
                "документ, либо когда это уместно подкрепляет доверие."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что за документ нужен"},
                    "caption": {
                        "type": "string",
                        "description": "Короткая подпись к документу (необязательно)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_follow_up",
            "description": (
                "Запланировать вежливое напоминание клиенту позже, когда он говорит «я подумаю» "
                "или не готов решить сейчас. Ты сам пишешь текст напоминания под контекст диалога."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Текст будущего напоминания клиенту (от первого лица).",
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "Точная дата-время в местном времени в формате ISO 8601 "
                            "(например 2026-07-03T15:00:00), если клиент назвал конкретный момент."
                        ),
                    },
                    "hours": {
                        "type": "number",
                        "description": "Или через сколько часов, если точное время не названо.",
                    },
                },
                "required": ["message"],
            },
        },
    },
]


def _tenge(amount: int) -> str:
    return f"{amount:,} ₸".replace(",", " ")


async def _list_products(session: AsyncSession) -> str:
    products = await active_products(session)
    if not products:
        return "Каталог пуст."
    return "\n".join(f"id={p.id} · {p.title} — {_tenge(p.price_kzt)}" for p in products)


async def _get_product(session: AsyncSession, product_id: int) -> str:
    p = await session.get(Product, product_id)
    if not p or not p.is_active:
        return "Товар не найден."
    return f"{p.title}\nЦена: {_tenge(p.price_kzt)}\n{(p.description or '').strip()}"


async def _create_payment(
    session: AsyncSession, user: User, product_id: int, quantity: int = 1
) -> str:
    product = await session.get(Product, product_id)
    if not product or not product.is_active:
        return "Товар не найден или недоступен."
    qty = max(1, int(quantity or 1))
    order = await orders_svc.create_order(session, user, product, quantity=qty)
    base = get_settings().base_url.rstrip("/")
    link = f"{base}/pay/{order.code}"
    return (
        f"Заказ {order.code} на {qty} шт. оформлен — сумма {_tenge(order.amount_kzt)}. "
        f"Отправь клиенту эту ссылку на оплату одной строкой: {link}"
    )


async def _check_payment(session: AsyncSession, user: User) -> str:
    order = (
        (
            await session.execute(
                select(Order).where(Order.user_id == user.id).order_by(Order.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if not order:
        return "У клиента ещё нет заказов."
    return (
        f"Заказ {order.code}: статус «{order.status.value}». "
        "Если оплата на проверке — попроси прислать чек или подождать подтверждения оператором."
    )


async def _search_knowledge(session: AsyncSession, query: str) -> str:
    items = await search_kb(session, query)
    if not items:
        return "В базе знаний ничего не найдено. Отвечай осторожно или позови оператора."
    return "\n\n".join(f"[{it.kind.value}] {it.title}\n{it.body}" for it in items)


async def _resend_access(session: AsyncSession, user: User, messenger: Messenger, tg_bot) -> str:
    """Re-deliver the real access (the course link from the product card) for the
    client's paid orders — so the bot never has to invent a link or point to a site."""
    orders = (
        (
            await session.execute(
                select(Order)
                .where(
                    Order.user_id == user.id,
                    Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLED]),
                )
                .order_by(Order.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not orders:
        return (
            "У клиента НЕТ оплаченных заказов — переотправлять доступ не за что. НЕ выдумывай "
            "ссылку и НЕ отправляй на сайт. Предложи выбрать курс и оформить оплату."
        )
    sent = 0
    for o in orders:
        product = await session.get(Product, o.product_id)
        if product is None:
            continue
        try:
            result = await build_fulfillment(product).grant(
                tg_bot=tg_bot, product=product, order=o, user=user
            )
            await messenger.send_text(user.external_id, result.message)
            sent += 1
        except Exception as e:  # noqa: BLE001 — one failure must not abort the rest
            logger.warning("resend_access failed for order %s: %s", o.code, e)
    if sent:
        return (
            f"Переотправил клиенту реальную ссылку-доступ по {sent} оплаченному заказу "
            "(из карточки товара). Скажи, что доступ отправлен — пусть проверит."
        )
    return "Не удалось переотправить доступ — позови оператора через escalate_to_operator."


async def _escalate(session: AsyncSession, user: User, reason: str) -> str:
    text = (
        "🆘 Нужен оператор.\n"
        f"Канал: {user.channel.value}\n"
        f"{client_block(user)}\n"
        f"Причина: {html.escape(reason or '')}"
    )
    # Карточкой, а не просто текстом: передача менеджеру — это заявка, её нужно
    # уметь взять в работу и увидеть, чем она кончилась.
    delivered = False
    try:
        lead = await leads_service.record_handoff(session, user, reason=reason, kind="оператор")
        delivered = bool(lead and lead.card_message_id)
    except Exception:  # noqa: BLE001 — клиента терять нельзя, ниже запасной путь
        logger.exception("escalate: карточка заявки не создана")
    if not delivered:
        delivered = await send_operator_message(session, text)
    if delivered:
        return "Оператор уведомлён. Скажи клиенту, что человек скоро подключится."
    # No operator chat configured — stay honest instead of pretending.
    return (
        "ВНИМАНИЕ: чат операторов не настроен, уведомление НЕ доставлено. "
        "Не утверждай клиенту, что оператора уже позвали. Вместо этого вежливо "
        "попроси его оставить телефон или удобный контакт, скажи, что менеджер "
        "свяжется с ним в ближайшее время, и постарайся помочь сам."
    )


async def _ask_admin(session: AsyncSession, user: User, question: str) -> str:
    """Forward a client question the bot can't answer to the admin chat, so a human can
    reply to the client in their dialog. The bot then tells the client it's checking —
    without inventing an answer."""
    text = (
        "❓ Вопрос клиента, которого НЕТ в базе — бот не смог ответить сам.\n"
        f"Канал: {user.channel.value}\n"
        f"{client_block(user)}\n"
        f"Вопрос: {html.escape(question or '')}\n"
        "➡️ Ответьте клиенту в его диалоге (Диалоги / топик) — он ждёт ответа."
    )
    # Бот упёрся в незнание — это тоже передача клиента человеку, значит заявка.
    delivered = False
    try:
        lead = await leads_service.record_handoff(
            session, user, reason=f"Вопрос без ответа: {question}", kind="вопрос"
        )
        delivered = bool(lead and lead.card_message_id)
    except Exception:  # noqa: BLE001
        logger.exception("ask_admin: карточка заявки не создана")
    # Админам всё равно шлём текст вопроса: отвечать на него им, а карточка —
    # для продавцов, чтобы клиент не потерялся, пока ищут ответ.
    delivered = await send_operator_message(session, text) or delivered
    if delivered:
        return (
            "Вопрос передан администратору. Скажи клиенту, что уточняешь у специалиста и "
            "скоро вернёшься с ответом. НЕ выдумывай ответ сам."
        )
    return (
        "ВНИМАНИЕ: чат админов не настроен, вопрос НЕ доставлен. Не выдумывай ответ — вежливо "
        "скажи клиенту, что уточнишь и вернёшься, и попроси оставить контакт для ответа."
    )


async def _save_contact(
    session: AsyncSession, user: User, name: str | None, phone: str | None, email: str | None
) -> str:
    if name:
        user.first_name = name
        user.name_confirmed = True  # client told us their name → the bot may address them by it
    if phone:
        user.phone = _resolve_phone(user, phone) or phone  # 'этот' → their WhatsApp number
    if email:
        user.email = email
    await session.flush()
    return "Контакт сохранён."


async def _schedule_follow_up(
    session: AsyncSession, user: User, message: str, hours: float | None, at: str | None = None
) -> str:
    tz = await get_local_tz(session)
    now = datetime.now(UTC)

    due: datetime | None = None
    if at:  # exact local time the client named, e.g. "2026-07-03T15:00:00"
        try:
            dt = datetime.fromisoformat(str(at).strip())
            due = (dt.replace(tzinfo=tz) if dt.tzinfo is None else dt).astimezone(UTC)
        except (ValueError, TypeError):
            due = None
    if due is None:  # relative fallback
        cfg = await get_setting(session, "followup") or {}
        default_hours = float(cfg.get("think_default_hours", 24) or 24)
        try:
            h = float(hours) if hours is not None else default_hours
        except (TypeError, ValueError):
            h = default_hours
        due = now + timedelta(hours=max(1.0, min(h, 24 * 14)))

    # Keep it sane: at least a minute out, at most 60 days.
    due = max(now + timedelta(minutes=1), min(due, now + timedelta(days=60)))
    text = (message or "").strip() or "Здравствуйте! Вы хотели подумать — подскажите, определились?"

    # Reuse a pending follow-up for this user (reschedule) instead of stacking messages.
    existing = (
        (
            await session.execute(
                select(Followup).where(Followup.user_id == user.id, Followup.sent.is_(False))
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        existing.due_at, existing.message, existing.kind = due, text, "think_over"
    else:
        session.add(Followup(user_id=user.id, kind="think_over", message=text, due_at=due))
    await session.flush()
    return f"Ок, напомню клиенту {due.astimezone(tz):%d.%m в %H:%M}."


def _channel_phone(user: User) -> str | None:
    """The client's own number derivable from the channel — for WhatsApp the external_id
    IS the phone number, so 'этот номер' resolves to it without the client retyping it."""
    if user.channel == Channel.WHATSAPP:
        digits = "".join(c for c in (user.external_id or "") if c.isdigit())
        return f"+{digits}" if len(digits) >= 8 else None
    return None


def _resolve_phone(user: User, phone: str | None) -> str | None:
    """Prefer a real number the client typed; if they answered vaguely ('этот', 'тот же',
    'с этого') or gave no digits, fall back to the number of the channel they write from."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) >= 8:
        return (phone or "").strip()
    return _channel_phone(user)


async def _request_callback(
    session: AsyncSession, user: User, name: str | None, phone: str | None, note: str | None
) -> str:
    if name:
        user.first_name = name
        user.name_confirmed = True  # client told us their name → the bot may address them by it
    resolved = _resolve_phone(user, phone)  # 'этот' / no digits → number of their own channel
    if resolved:
        user.phone = resolved
    await session.flush()
    text = (
        "📞 Клиент просит перезвонить.\n"
        f"Имя: {html.escape(name or user.full_name)}\n"
        f"Телефон: {html.escape(resolved or 'не указан — см. контакт ниже')}\n"
        f"Канал: {user.channel.value}\n"
        f"{client_block(user)}"
    )
    if note:
        text += f"\nВопрос: {html.escape(note)}"
    delivered = False
    try:
        lead = await leads_service.record_handoff(
            session,
            user,
            reason="Просит перезвонить" + (f": {note}" if note else ""),
            kind="звонок",
        )
        delivered = bool(lead and lead.card_message_id)
    except Exception:  # noqa: BLE001
        logger.exception("request_callback: карточка заявки не создана")
    if not delivered:
        delivered = await send_operator_message(session, text)
    if delivered:
        return "Заявка на звонок передана менеджеру. Скажи клиенту, что скоро перезвонят."
    return (
        "ВНИМАНИЕ: чат для уведомлений не настроен, заявка на звонок НЕ доставлена. "
        "Скажи клиенту, что менеджер свяжется в ближайшее время, и убедись, что номер записан."
    )


async def _preferred_media(session, user, query: str, kind: str | None = None):
    """Материал, привязанный к товару разговора, если он подходит под запрос.

    Смотрим сюда ДО общей библиотеки: фото одного товара не должно улетать
    клиенту, который спрашивает про другой. Ничего не подошло —
    возвращаем None, и работает обычный поиск по всей медиатеке.
    """
    from app.sales.product_rules import active_product, linked_assets

    product = await active_product(session, user.id)
    if product is None:
        return None
    assets = await linked_assets(session, product.id)
    if kind:
        assets = [a for a in assets if (a.kind or "image") == kind]
    if not assets:
        return None
    words = [w for w in (query or "").lower().split() if len(w) > 2]
    if words:
        for a in assets:
            haystack = f"{a.title} {a.tags} {a.trigger}".lower()
            if any(w in haystack for w in words):
                return a
    return assets[0]

async def _send_image(
    session: AsyncSession, user: User, messenger: Messenger, query: str, caption: str | None
) -> str:
    q = (query or "").strip()
    asset = None
    if q.isdigit():  # the model picked a picture by its NUMBER from the prompt list
        # Numbering must match the prompt's list, which contains PICTURES only.
        gallery = [m for m in await active_media(session) if (m.kind or "image") == "image"]
        idx = int(q) - 1
        if 0 <= idx < len(gallery):
            asset = gallery[idx]
    if asset is None:  # сначала материалы этого товара, потом вся библиотека
        asset = await _preferred_media(session, user, q, kind="image")
    if asset is None:
        found = await search_media(session, q, limit=1)
        asset = found[0] if found else None
    if asset is None:
        return (
            "В библиотеке нет подходящей картинки. Не выдумывай ссылку — "
            "просто продолжи общение текстом."
        )
    if (asset.kind or "image") != "image":
        # Asked for a picture but the best match is a circle/video/voice/link — deliver it
        # properly instead of pushing an mp4 through send_photo.
        return await _send_asset(session, user, messenger, query, caption)
    if await media_sent_recently(session, user.id, asset.title):
        return (
            f"Картинка «{asset.title}» уже отправлена этому клиенту сегодня. НЕ дублируй её — "
            "продолжи общение текстом."
        )
    try:
        await messenger.send_image(
            user.external_id, public_url(asset.filename), (caption or "").strip()
        )
    except Exception as e:  # noqa: BLE001 — delivery failure must not crash the turn
        logger.warning("send_image delivery failed: %s", e)
        return "Не удалось отправить картинку клиенту."
    await record_sent_media(
        session,
        user,
        title=asset.title,
        word="картинка",
        url=public_url(asset.filename),
        kind="photo",
        caption=caption or "",
    )
    return f"Картинка «{asset.title}» отправлена клиенту."


# Media-library kind → (how «Диалоги»/Telegram store it, what the operator should read).
# A circle is an ordinary mp4 — only its label differs.
_ASSET_MEDIA = {
    "image": ("photo", "картинка"),
    "circle": ("video", "кружок"),
    "video": ("video", "видео"),
    "voice": ("voice", "голосовое"),
}


async def _send_asset(
    session: AsyncSession, user: User, messenger: Messenger, query: str, caption: str | None
) -> str:
    """Send any asset type (image/circle/video/voice/link) from the media library."""
    q = (query or "").strip()
    asset = await _preferred_media(session, user, q)
    if asset is None:
        found = await search_media(session, q, limit=1)
        asset = found[0] if found else None
    if asset is None:
        return (
            "В библиотеке нет подходящего материала. Не выдумывай ссылку — "
            "просто продолжи общение текстом."
        )
    if await media_sent_recently(session, user.id, asset.title):
        return (
            f"Материал «{asset.title}» уже отправлен этому клиенту сегодня. НЕ отправляй его "
            "повторно — продолжи общение текстом."
        )
    link_text = ""
    try:
        cap = (caption or "").strip()
        if asset.kind == "image":
            await messenger.send_image(user.external_id, public_url(asset.filename), cap)
        elif asset.kind == "circle":
            await messenger.send_video_note(user.external_id, public_url(asset.filename), cap)
        elif asset.kind == "video":
            await messenger.send_media(
                user.external_id,
                url=public_url(asset.filename),
                mime=asset.mime or "video/mp4",
                kind="video",
                caption=cap,
            )
        elif asset.kind == "voice":
            await messenger.send_media(
                user.external_id,
                url=public_url(asset.filename),
                mime=asset.mime or "audio/ogg",
                kind="voice",
                caption=cap,
            )
        elif asset.kind == "link":
            link_text = (
                f"{cap}\n{asset.url}".strip() if cap else f"{asset.title}\n{asset.url}".strip()
            )
            await messenger.send_text(user.external_id, link_text)
    except Exception as e:  # noqa: BLE001 — delivery failure must not crash the turn
        logger.warning("send_asset delivery failed: %s", e)
        return "Не удалось отправить материал клиенту."
    if link_text:  # a link is plain text — there is no attachment to preview
        await record_sent_text(session, user, link_text)
    else:
        kind, word = _ASSET_MEDIA.get(asset.kind, ("document", "материал"))
        await record_sent_media(
            session,
            user,
            title=asset.title,
            word=word,
            url=public_url(asset.filename),
            kind=kind,
            caption=cap,
        )
    return f"Материал «{asset.title}» ({asset.kind}) отправлен клиенту."


async def _send_document(
    session: AsyncSession, user: User, messenger: Messenger, query: str, caption: str | None
) -> str:
    q = (query or "").strip()
    doc = None
    if q.isdigit():  # the model picked a document by its NUMBER from the prompt list
        gallery = await active_documents(session)
        idx = int(q) - 1
        if 0 <= idx < len(gallery):
            doc = gallery[idx]
    if doc is None:  # else match the query words against title + tags + category
        found = await search_documents(session, q, limit=1)
        doc = found[0] if found else None
    if doc is None:
        return (
            "В библиотеке нет подходящего документа. Не выдумывай ссылку — "
            "просто продолжи общение текстом."
        )
    if await media_sent_recently(session, user.id, doc.title):
        return (
            f"Документ «{doc.title}» уже отправлен этому клиенту сегодня. НЕ дублируй его — "
            "продолжи общение текстом."
        )
    try:
        await messenger.send_media(
            user.external_id,
            url=public_url(doc.filename),
            mime=doc.mime,
            kind=doc.kind,
            filename=doc.title,
            caption=(caption or "").strip(),
        )
    except Exception as e:  # noqa: BLE001 — delivery failure must not crash the turn
        logger.warning("send_document delivery failed: %s", e)
        return "Не удалось отправить документ клиенту."
    await record_sent_media(
        session,
        user,
        title=doc.title,
        word="документ",
        url=public_url(doc.filename),
        kind=doc.kind or "document",  # already 'document' | 'photo' — what the UI renders
        caption=caption or "",
    )
    return f"Документ «{doc.title}» отправлен клиенту."


async def run_tool(
    name: str,
    args: dict,
    *,
    session: AsyncSession,
    user: User,
    messenger: Messenger,
    tg_bot=None,
) -> str:
    """Execute a tool by name and return a string result for the model."""
    try:
        if name == "list_products":
            return await _list_products(session)
        if name == "get_product":
            return await _get_product(session, int(args["product_id"]))
        if name == "create_payment":
            return await _create_payment(
                session, user, int(args["product_id"]), int(args.get("quantity") or 1)
            )
        if name == "check_payment":
            return await _check_payment(session, user)
        if name == "resend_access":
            return await _resend_access(session, user, messenger, tg_bot)
        if name == "search_knowledge":
            return await _search_knowledge(session, args.get("query", ""))
        if name == "escalate_to_operator":
            return await _escalate(session, user, args.get("reason", ""))
        if name == "ask_admin":
            return await _ask_admin(session, user, args.get("question", ""))
        if name == "save_contact":
            return await _save_contact(
                session, user, args.get("name"), args.get("phone"), args.get("email")
            )
        if name == "request_callback":
            return await _request_callback(
                session, user, args.get("name"), args.get("phone"), args.get("note")
            )
        if name == "send_image":
            return await _send_image(
                session, user, messenger, args.get("query", ""), args.get("caption")
            )
        if name == "send_asset":
            return await _send_asset(
                session, user, messenger, args.get("query", ""), args.get("caption")
            )
        if name == "send_document":
            return await _send_document(
                session, user, messenger, args.get("query", ""), args.get("caption")
            )
        if name == "schedule_follow_up":
            return await _schedule_follow_up(
                session, user, args.get("message", ""), args.get("hours"), args.get("at")
            )
        return f"Неизвестный инструмент: {name}"
    except Exception as e:  # noqa: BLE001 — surface to the model, don't crash the turn
        logger.exception("Tool %s failed", name)
        return f"Ошибка инструмента {name}: {e}"
