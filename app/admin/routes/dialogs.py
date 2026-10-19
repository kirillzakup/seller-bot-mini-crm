"""Admin «Диалоги»: omnichannel inbox — view every conversation and reply as a
human operator across Telegram / WhatsApp / Instagram.

Reading uses the persisted ``messages`` table. Sending delivers via the channel's
Messenger and records an ``operator`` turn; taking over auto-pauses the bot for
that chat (``User.bot_paused``) so the AI and the human don't both answer. The
thread auto-refreshes via HTMX polling for a near-live, WhatsApp-like feel.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.channels.meta import get_reengage_template
from app.channels.registry import get_messenger
from app.db.base import get_session
from app.db.models import Message, User
from app.db.models.enums import Channel, ConversationRole
from app.sales import debounce
from app.sales.conversation import save_message, wa_window_open
from app.services import inbox as inbox_svc
from app.services import media as media_svc
from app.services.learning import capture_operator_answer

_MEDIA_LABEL = {
    "photo": "📷 Фото",
    "video": "🎥 Видео",
    "voice": "🎤 Голосовое",
    "document": "📎 Файл",
}

router = APIRouter(prefix="/dialogs")
logger = logging.getLogger(__name__)


def _delivery_warning(user: User) -> str:
    """Shown as the transient banner when a send did NOT reach the client."""
    if user.channel == Channel.WHATSAPP:
        return (
            "⚠️ WhatsApp не принял сообщение к доставке. Скорее всего клиент не писал больше "
            "24 часов — тогда WhatsApp разрешает отправлять только шаблонные сообщения. "
            "Попросите клиента написать первым (например, «Напишите “+” в ответ»)."
        )
    return "⚠️ Сообщение не удалось доставить в мессенджере — проверьте подключение канала."


@router.get("/ping")
async def ping(
    since: int = 0,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Lightweight poll for the notifier: newest message id + count of new inbound."""
    max_id = (
        await session.execute(select(func.coalesce(func.max(Message.id), 0)))
    ).scalar_one()
    new = (
        await session.execute(
            select(func.count(Message.id)).where(
                Message.id > since, Message.role == ConversationRole.USER
            )
        )
    ).scalar_one()
    return {"max_id": int(max_id), "new": int(new)}


@router.get("/counts")
async def counts(
    date_from: str = "",
    date_to: str = "",
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Per-channel client counts for the «Диалоги» header — how many people WROTE from each
    channel (Telegram / Instagram / WhatsApp). Optional date_from/date_to (YYYY-MM-DD) scope
    it to clients who sent a message in that period."""

    def _day(s: str) -> datetime | None:
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d") if s.strip() else None
        except ValueError:
            return None

    conds = [Message.role == ConversationRole.USER]  # «написало» = inbound client messages
    fdt, tdt = _day(date_from), _day(date_to)
    if fdt is not None:
        conds.append(Message.created_at >= fdt)
    if tdt is not None:
        conds.append(Message.created_at < tdt + timedelta(days=1))
    rows = (
        await session.execute(
            select(User.channel, func.count(func.distinct(Message.user_id)))
            .join(Message, Message.user_id == User.id)
            .where(*conds)
            .group_by(User.channel)
        )
    ).all()
    by = {(ch.value if hasattr(ch, "value") else str(ch)): int(n) for ch, n in rows}
    return {
        "telegram": by.get("telegram", 0),
        "instagram": by.get("instagram", 0),
        "whatsapp": by.get("whatsapp", 0),
    }


async def _conversations(session: AsyncSession) -> list[dict]:
    last_ids = select(func.max(Message.id)).group_by(Message.user_id)
    last_msgs = (
        (await session.execute(select(Message).where(Message.id.in_(last_ids)))).scalars().all()
    )
    last_msgs.sort(key=lambda m: m.id, reverse=True)
    last_msgs = last_msgs[:300]
    ids = [m.user_id for m in last_msgs]
    users = {
        u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()
    }
    return [{"user": users[m.user_id], "last": m} for m in last_msgs if m.user_id in users]


async def _messages(session: AsyncSession, user_id: int) -> list[Message]:
    return (
        (
            await session.execute(
                select(Message).where(Message.user_id == user_id).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )


@router.get("")
async def inbox(
    request: Request,
    u: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    active = await session.get(User, u) if u else None
    if active is not None and active.unread:
        active.unread = False  # opening a chat clears its unread flag
        await session.flush()
    convos = await _conversations(session)
    msgs = await _messages(session, u) if active else []
    # WhatsApp only: surface a «Разбудить» button when the 24h window is closed and a
    # re-engagement template is configured (otherwise the button would only ever fail).
    wa_open, reengage_ready = True, False
    if active is not None and active.channel == Channel.WHATSAPP:
        wa_open = await wa_window_open(session, active.id)
        reengage_ready = (await get_reengage_template(session)) is not None
    return templates.TemplateResponse(
        request,
        "dialogs.html",
        {
            "active": "dialogs",
            "convos": convos,
            "au": active,
            "msgs": msgs,
            "wa_window_open": wa_open,
            "reengage_ready": reengage_ready,
        },
    )


@router.get("/{user_id}/thread")
async def thread(
    user_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    msgs = await _messages(session, user_id)
    return templates.TemplateResponse(request, "_thread.html", {"au": user, "msgs": msgs})


@router.post("/{user_id}/send")
async def send(
    user_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    text: str = Form(""),
    file: UploadFile | None = File(None),
):
    user = await session.get(User, user_id)
    text = (text or "").strip()
    has_file = file is not None and bool(file.filename)
    err = ""
    if user is not None and (text or has_file):
        # The operator is answering this chat → kill any bot reply still pending in the
        # debounce timer, so the bot doesn't ALSO answer the same message (double reply).
        debounce.cancel(user.id)
        try:
            messenger = await get_messenger(session, user.channel)
        except Exception:  # noqa: BLE001
            messenger = None
            logger.warning("Operator send: no messenger for user %s", user_id)

        if has_file:
            err = await _send_file(session, user, messenger, file, caption=text)
        else:
            err = await _send_operator_text(session, user, messenger, text)
    msgs = await _messages(session, user_id)
    return templates.TemplateResponse(
        request, "_thread.html", {"au": user, "msgs": msgs, "err": err}
    )


async def _send_operator_text(session, user, messenger, text: str) -> str:
    """Send the operator's text to the client. Returns a warning string (rendered as
    a banner) when the channel didn't actually deliver it, else ''."""
    # Bot active (not manually paused) → RE-VOICE the admin's answer as the bot (rephrase in
    # its style + send), THEN take over: pause the bot so it doesn't ALSO auto-answer the
    # client (the double-reply bug). Manual takeover (bot paused) → send the text verbatim.
    if not user.bot_paused and messenger is not None:
        await save_message(session, user.id, "operator", text)  # keep the admin's exact wording
        try:
            await capture_operator_answer(session, user.id, text)
        except Exception:  # noqa: BLE001
            logger.debug("KB capture skipped for user %s", user.id)
        from app.sales import ai_manager

        sent = await ai_manager.relay_operator_answer(
            session, user=user, messenger=messenger, answer=text
        )
        user.bot_paused = True  # operator answered → take over, bot stops double-replying
        return "" if sent else _delivery_warning(user)
    mid = None
    if messenger is not None:
        try:
            mid = await messenger.send_text(user.external_id, text)
        except Exception:  # noqa: BLE001
            logger.warning("Operator send failed for user %s", user.id)
    op_msg = await save_message(session, user.id, "operator", text)
    op_msg.status = "sent" if mid else "failed"
    if mid:
        op_msg.channel_mid = str(mid)
    user.bot_paused = True  # human took over → pause the bot for this chat
    await inbox_svc.mirror_outgoing(session, user, text, who="operator")
    if mid:  # only teach the KB from an answer that actually reached the client
        try:
            await capture_operator_answer(session, user.id, text)
        except Exception:  # noqa: BLE001
            logger.debug("KB capture skipped for user %s", user.id)
    return "" if mid else _delivery_warning(user)


async def _send_file(session, user, messenger, file: UploadFile, *, caption: str) -> str:
    """Save an operator's uploaded file, send it to the client, record the turn.
    Returns an error string (shown as a transient banner) or '' on success."""
    # Reject oversized files by declared size BEFORE reading them into memory (DoS guard).
    if file.size and file.size > media_svc.operator_max_bytes():
        return f"Файл слишком большой — максимум {media_svc.operator_max_mb()} МБ."
    saved = media_svc.save_operator_file(await file.read(), file.content_type or "", file.filename)
    if saved is None:
        return "Файл не отправлен: неподдерживаемый тип или больше 100 МБ."
    mid = None
    if messenger is not None:
        try:
            mid = await messenger.send_media(
                user.external_id,
                url=saved["url"],
                mime=saved["mime"],
                kind=saved["kind"],
                filename=file.filename,
                caption=caption,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Operator media send failed for user %s", user.id)
    op_msg = await save_message(
        session, user.id, "operator", caption or _MEDIA_LABEL.get(saved["kind"], "📎 Файл")
    )
    op_msg.status = "sent" if mid else "failed"
    op_msg.media_url = saved["url"]
    op_msg.media_kind = saved["kind"]
    if mid:
        op_msg.channel_mid = str(mid)
    user.bot_paused = True  # human took over → pause the bot for this chat
    await inbox_svc.mirror_outgoing(
        session,
        user,
        caption or "",
        who="operator",
        media_url=saved["url"],
        media_kind=saved["kind"],
    )
    return "" if mid else _delivery_warning(user)


@router.post("/{user_id}/bot")
async def toggle_bot(
    user_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is not None:
        user.bot_paused = not user.bot_paused
    return RedirectResponse(f"/admin/dialogs?u={user_id}", status_code=303)


@router.post("/{user_id}/reminders")
async def toggle_reminders(
    user_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Per-client on/off for silence reminders (дожим)."""
    user = await session.get(User, user_id)
    if user is not None:
        user.reminders_enabled = not user.reminders_enabled
    return RedirectResponse(f"/admin/dialogs?u={user_id}", status_code=303)


@router.post("/{user_id}/promos")
async def toggle_promos(
    user_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Per-client on/off for congratulations + informational broadcasts."""
    user = await session.get(User, user_id)
    if user is not None:
        user.promos_enabled = not user.promos_enabled
    return RedirectResponse(f"/admin/dialogs?u={user_id}", status_code=303)


@router.post("/{user_id}/reengage")
async def reengage(
    user_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Manually send the approved WhatsApp template to re-open a closed 24-hour window,
    so the client can reply and normal free-form messaging resumes."""
    user = await session.get(User, user_id)
    err = ""
    if user is None or user.channel != Channel.WHATSAPP:
        err = "«Разбудить» доступно только для WhatsApp."
    elif not (tmpl := await get_reengage_template(session)):
        err = "Шаблон не настроен — задайте его в «Каналы»."
    else:
        mid = None
        try:
            messenger = await get_messenger(session, user.channel)
            mid = await messenger.send_template(
                user.external_id, name=tmpl["name"], language=tmpl["language"]
            )
        except Exception:  # noqa: BLE001
            logger.warning("Reengage template send failed for user %s", user_id)
        if mid:
            m = await save_message(
                session, user.id, "operator", tmpl["text"] or f"[шаблон {tmpl['name']}]"
            )
            m.status = "sent"
            m.channel_mid = str(mid)
            if user.reengaged_at is None:
                user.reengaged_at = datetime.now(UTC)
        else:
            err = (
                "Шаблон не отправлен — проверьте, что он одобрен в Meta и правильно "
                "указан в «Каналы»."
            )
    msgs = await _messages(session, user_id)
    return templates.TemplateResponse(
        request, "_thread.html", {"au": user, "msgs": msgs, "err": err}
    )


@router.post("/{user_id}/unread")
async def mark_unread(
    user_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Flag a chat unread (WhatsApp-style) so it stands out in the list until reopened."""
    user = await session.get(User, user_id)
    if user is not None:
        user.unread = True
    return RedirectResponse("/admin/dialogs", status_code=303)


@router.post("/{user_id}/message/{message_id}/pin")
async def toggle_pin(
    user_id: int,
    message_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Pin/unpin a message to the top of the thread (panel-only)."""
    msg = await session.get(Message, message_id)
    if msg is not None and msg.user_id == user_id:
        msg.pinned = not msg.pinned
        await session.flush()
    user = await session.get(User, user_id)
    msgs = await _messages(session, user_id)
    return templates.TemplateResponse(request, "_thread.html", {"au": user, "msgs": msgs})


@router.post("/{user_id}/message/{message_id}/react")
async def react(
    user_id: int,
    message_id: int,
    request: Request,
    emoji: str = Form(""),
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Set/clear an emoji reaction on a message. Tapping the same emoji clears it. Where
    the message has a platform id, the reaction is also sent to the client (WhatsApp/
    Telegram); Instagram keeps it panel-only."""
    msg = await session.get(Message, message_id)
    user = await session.get(User, user_id)
    if msg is not None and msg.user_id == user_id:
        msg.reaction = "" if (msg.reaction or "") == emoji else emoji
        await session.flush()
        if user is not None and msg.channel_mid:
            try:
                messenger = await get_messenger(session, user.channel)
                await messenger.send_reaction(user.external_id, msg.channel_mid, msg.reaction or "")
            except Exception:  # noqa: BLE001 — reaction still shows in the panel
                logger.debug("Channel reaction skipped for message %s", message_id)
    msgs = await _messages(session, user_id)
    return templates.TemplateResponse(request, "_thread.html", {"au": user, "msgs": msgs})


# mime to declare when re-sending forwarded media by kind (send_media picks the WA
# type from ``kind``; the mime is a best-effort content-type for the other channels).
_KIND_MIME = {
    "photo": "image/jpeg",
    "video": "video/mp4",
    "voice": "audio/ogg",
    "document": "application/octet-stream",
}


@router.post("/forward")
async def forward(
    request: Request,
    message_id: int = Form(...),
    targets: list[int] = Form(default=[]),
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Forward one message's text/media to several chats at once (any channel)."""
    src = await session.get(Message, message_id)
    if src is None:
        return templates.TemplateResponse(
            request, "_forward_result.html", {"sent": 0, "total": len(targets)}
        )
    is_label = bool(src.media_url) and src.content in _MEDIA_LABEL.values()
    caption = "" if is_label else (src.content or "")
    sent = 0
    for tid in targets:
        target = await session.get(User, tid)
        if target is None:
            continue
        try:
            messenger = await get_messenger(session, target.channel)
        except Exception:  # noqa: BLE001
            messenger = None
        mid = None
        try:
            if src.media_url and messenger is not None:
                mid = await messenger.send_media(
                    target.external_id,
                    url=src.media_url,
                    mime=_KIND_MIME.get(src.media_kind or "document", "application/octet-stream"),
                    kind=src.media_kind or "document",
                    caption=caption,
                )
            elif caption and messenger is not None:
                mid = await messenger.send_text(target.external_id, caption)
        except Exception:  # noqa: BLE001
            logger.warning("Forward to user %s failed", tid)
            continue
        label = caption or _MEDIA_LABEL.get(src.media_kind or "", "📎 Файл")
        fwd = await save_message(session, target.id, "operator", f"↪ {label}")
        fwd.status = "sent" if mid else "failed"  # mirrors normal send: no mid = not delivered
        if src.media_url:
            fwd.media_url = src.media_url
            fwd.media_kind = src.media_kind
        if mid:
            fwd.channel_mid = str(mid)
        target.bot_paused = True  # a human just messaged them → don't let the bot butt in
        if mid:
            sent += 1  # only count targets that actually received it
    return templates.TemplateResponse(
        request, "_forward_result.html", {"sent": sent, "total": len(targets)}
    )


@router.post("/{user_id}/message/{message_id}/delete")
async def delete_message(
    user_id: int,
    message_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Delete one message from a conversation. Drops our stored copy — so it leaves
    both the panel AND the bot's memory (the AI reloads the ``messages`` table) — and,
    where the channel allows it (Telegram), unsends it from the client's chat too.
    WhatsApp/Instagram have no delete API, so there it only leaves our records."""
    user = await session.get(User, user_id)
    msg = await session.get(Message, message_id)
    if msg is not None and msg.user_id == user_id:
        if user is not None and msg.channel_mid:
            try:
                messenger = await get_messenger(session, user.channel)
                await messenger.delete_message(user.external_id, msg.channel_mid)
            except Exception:  # noqa: BLE001 — client-side unsend is best-effort
                logger.debug("Channel delete skipped for message %s", message_id)
        await session.delete(msg)
        await session.flush()
    msgs = await _messages(session, user_id)
    return templates.TemplateResponse(request, "_thread.html", {"au": user, "msgs": msgs})
