"""Shared inbound dispatch: turn an IncomingMessage into action.

Used by every channel. Routes receipts (image while an order awaits payment) to
the manual-confirmation flow, and everything else to the AI manager.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import IncomingComment, IncomingMessage
from app.channels.meta import was_bot_sent
from app.channels.registry import get_messenger
from app.db.models import MediaAsset, User
from app.db.models.content import Message
from app.db.models.enums import Channel
from app.sales import ai_manager, debounce, vision
from app.sales import comments as comments_svc
from app.sales.conversation import save_message
from app.services import expo_push, leads_service, showcase
from app.services import inbox as inbox_svc
from app.services import orders as orders_svc
from app.services import receipts as receipts_svc
from app.services.events import log_event
from app.services.media import public_url, save_inbound_audio, save_operator_file
from app.services.notify import notify_manual_receipt
from app.services.push import send_push
from app.services.settings_service import get_setting
from app.services.users import get_or_create_contact
from app.voice import elevenlabs as voice

logger = logging.getLogger(__name__)

_VOICE_DISABLED = "Голосовые пока не подключены — напишите, пожалуйста, текстом 🙏"
_VOICE_UNCLEAR = "Не разобрал аудио 🤔 Повторите или напишите текстом."
_REACTION_THANKS = "Спасибо за вашу реакцию! 🤗"
_STORY_MENTION_THANKS = "Спасибо, что отметили нас в сторис! 🤗"
# Injected for story replies so the bot answers the question instead of cold-opening.
_STORY_SITUATION = (
    "Клиент ответил на вашу сторис. Это НЕ начало сценария продаж: не запускай "
    "приветственный скрипт и не представляйся заново. Если в сообщении есть вопрос — "
    "ответь по существу, опираясь на базу знаний (search_knowledge); если точного ответа "
    "нет — коротко скажи, что менеджер скоро уточнит и ответит. Будь тёплым и кратким."
)


async def _enrich_contact(session: AsyncSession, messenger, user) -> None:
    """Fill in an Instagram contact's @username + display name once (an IGSID alone
    isn't reachable, so operators can't find the person). Fetched only while the
    username is still missing; best-effort."""
    if user.username or user.channel != Channel.INSTAGRAM:
        return
    try:
        profile = await messenger.fetch_profile(user.external_id)
    except Exception:  # noqa: BLE001 — enrichment is a nicety, never block the message
        profile = None
    if not profile:
        return
    changed = False
    if profile.get("username"):
        user.username = str(profile["username"])
        changed = True
    name = (profile.get("name") or "").strip()
    if name and not user.first_name:
        user.first_name = name
        changed = True
    if changed:
        await session.flush()


async def _record_incoming_file(session, user, messenger, msg: IncomingMessage) -> dict:
    """Download a client's inbound image/PDF once, store it under /media, save it as a
    «Диалоги» message row (with a playable/downloadable preview) AND mirror it into the
    operator topic. Returns ``{"ref", "bytes", "is_pdf"}`` for the caller (receipt logic).

    Client images used to reach ONLY the Telegram mirror — never the web panel — so the
    team couldn't see receipts there. This is the single place that fixes that.
    """
    att = next((a for a in msg.attachments if a.get("kind") in ("image", "document")), {})
    data = await messenger.download_media(att) if att else None
    is_pdf = att.get("kind") == "document"
    mime = att.get("mime") or ("application/pdf" if is_pdf else "image/jpeg")
    fname = att.get("filename") or ("receipt.pdf" if is_pdf else "photo.jpg")
    saved = save_operator_file(data, mime, fname) if data else None
    label = msg.text or ("📎 Документ" if is_pdf else "🖼 Изображение")
    # media_url: our stored /media copy (WhatsApp) or the direct CDN url (Instagram).
    media_url = (saved["url"] if saved else None) or att.get("url")
    await save_message(
        session, user.id, "user", label,
        channel_mid=msg.external_message_id,
        media_url=media_url,
        media_kind=("document" if is_pdf else "photo") if media_url else None,
    )
    # Mirror into the operator topic. A PDF can't render as a Telegram photo → label only;
    # for an image _incoming_photo returns the per-channel form (IG url / TG file_id / WA bytes).
    photo = None if is_pdf else await _incoming_photo(messenger, msg)
    await inbox_svc.mirror_incoming(session, user, msg.text or label, photo=photo)
    ref = (saved["url"] if saved else "") or att.get("file_id") or att.get("url") or ""
    return {"ref": ref, "bytes": data, "is_pdf": is_pdf}


async def _incoming_photo(messenger, msg: IncomingMessage):
    """A Telegram-sendable photo for a client's inbound image, to mirror into the
    operator topic (IG = URL, Telegram = file_id, WhatsApp = downloaded bytes)."""
    ref = next((a for a in msg.attachments if a.get("kind") == "image"), None)
    if not ref:
        return None
    if msg.channel == Channel.INSTAGRAM and ref.get("url"):
        return ref["url"]
    if msg.channel == Channel.TELEGRAM and ref.get("file_id"):
        return ref["file_id"]
    if msg.channel == Channel.WHATSAPP and ref.get("file_id"):
        try:
            data = await messenger.download_media({"file_id": ref["file_id"]})
        except Exception:  # noqa: BLE001
            data = None
        if data:
            from aiogram.types import BufferedInputFile

            return BufferedInputFile(data, "image.jpg")
    return None


async def _already_have_own_text(session: AsyncSession, user_id: int, text: str) -> bool:
    """True if this exact text is already stored as one of our own recent outbound turns
    (the bot's ``assistant`` reply, or a prior copy of this same echo).

    Instagram echoes every outbound message back as a webhook; we normally recognise our
    own via the in-memory ``was_bot_sent`` cache, but that cache is volatile (lost on
    restart, and a small global ring shared across all chats), so an echo can slip through
    and get mis-saved as a phantom ``operator`` message — the message appears twice in the
    dialog. The bot always persists its reply as an ``assistant`` row *before* the echo
    arrives, so a quick look at the last few turns is a restart-proof, race-proof backstop.
    A genuine human-operator reply typed in the IG app is new text and won't match.
    """
    rows = (
        await session.execute(
            select(Message.role, Message.content)
            .where(Message.user_id == user_id)
            .order_by(Message.id.desc())
            .limit(12)
        )
    ).all()
    return any(
        role in ("assistant", "operator") and (content or "").strip() == text
        for role, content in rows
    )


async def _capture_ad_referral(session: AsyncSession, user: User, msg: IncomingMessage) -> None:
    """Record which Meta ad a lead came from.

    Click-to-WhatsApp / Click-to-Direct ads attach a ``referral`` block (ad id, headline,
    ctwa_clid) to the first message a client sends after tapping the ad. First touch keeps
    the credit, so a later ad can't steal attribution; every click is still logged as an
    event for analytics.
    """
    raw = msg.raw if isinstance(msg.raw, dict) else {}
    ref = raw.get("referral")
    if not isinstance(ref, dict) or not ref:
        return
    ad_id = str(ref.get("source_id") or "").strip()
    headline = str(ref.get("headline") or "").strip()
    label = " · ".join(p for p in (headline, f"ad {ad_id}" if ad_id else "") if p)[:255]
    if not label:
        return
    if not user.ad_ref:  # first touch keeps the credit
        user.ad_ref = label
        if not user.source and ad_id:
            user.source = f"ad:{ad_id}"[:64]
        await session.flush()
    await log_event(session, "ad_click", user_id=user.id, data=ref)
    logger.info("ad referral captured for user %s: %s", user.id, label)


_OPT_OUT_RE = re.compile(
    r"вопрос\s+закрыт|вопрос\s+реш[её]н|тема\s+закрыт|больше\s+не\s+пиши|"
    r"не\s+пишите\s+(мне|больше)|отпиш\w*\s+меня|прекратите\s+писать|"
    r"купил\w*\s+в\s+другом|не\s+беспокойте|снимите\s+с\s+рассылк",
    re.I,
)


async def _maybe_opt_out(session: AsyncSession, user, text: str) -> bool:
    """Клиент сказал «вопрос закрыт» → выключаем напоминания и рассылки."""
    if not text or not _OPT_OUT_RE.search(text):
        return False
    if not user.reminders_enabled and not user.promos_enabled:
        return True
    user.reminders_enabled = False
    user.promos_enabled = False
    await session.flush()
    await log_event(session, "opt_out", user_id=user.id, data={"text": text[:200]})
    logger.info("Opt-out for user %s: %s", user.id, text[:80])
    return True


async def dispatch_incoming(session: AsyncSession, msg: IncomingMessage) -> None:
    user = await get_or_create_contact(
        session,
        channel=msg.channel,
        external_id=msg.external_id,
        first_name=msg.name,
        username=msg.username,
        reset_reminders=not msg.is_echo,
    )

    # Echo — the business itself sent this (owner from the IG app, or the bot). Skip our
    # own sends; save the owner's manual replies so the bot keeps context when it resumes.
    if msg.is_echo:
        echo_text = (msg.text or "").strip()
        if not echo_text:
            return
        if was_bot_sent(msg.external_id, echo_text):
            return  # fast path: recognised via the in-memory cache
        if await _already_have_own_text(session, user.id, echo_text):
            # In-memory cache missed (restart / eviction / race) but the DB shows it's
            # our own message — don't duplicate it as a phantom operator reply.
            logger.info("echo dedup: DB backstop caught own message for user %s", user.id)
            return
        await save_message(session, user.id, "operator", echo_text)
        return

    # Meta click-to-message ads (Click-to-WhatsApp / Click-to-Direct) attach a `referral`
    # block to the first message after the tap — record which creative paid off.
    await _capture_ad_referral(session, user, msg)

    messenger = await get_messenger(session, msg.channel)
    # Resolve an Instagram contact's @username + name once, so operators can reach
    # them (an IGSID alone isn't clickable).
    await _enrich_contact(session, messenger, user)

    # The client just wrote → they've seen our earlier messages. Mark them read (a
    # reliable receipt on every channel, incl. Telegram which has no read events).
    await receipts_svc.mark_read_for_user(session, user.id)

    # Просят показать фото, объекты или каталог — отправляем сразу и сами.
    # Модель эту просьбу исполняла через раз: на пять одинаковых запросов за сутки
    # пришли то портфолио, то ссылка на Instagram, а дважды — ничего.
    if showcase.is_photo_request(msg.text) and not user.bot_paused:
        await save_message(session, user.id, "user", msg.text, channel_mid=msg.external_message_id)
        await inbox_svc.mirror_incoming(session, user, msg.text)
        try:
            await showcase.send_showcase(session, user, messenger)
        except Exception:  # noqa: BLE001 — молчать в ответ на просьбу нельзя
            logger.exception("Витрину не отправили пользователю %s", user.id)
        # Заявку тоже заводим: человек смотрит объекты — им пора заняться менеджеру.
        try:
            await leads_service.record_handoff(
                session, user, reason=f"Просил фото: {msg.text}", kind="фото"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Заявку по запросу фото не создали для %s", user.id)
        return

    # Reaction, or a story mention/reshare with no text → warm thanks, never the sales
    # script (so an emoji on a story doesn't cold-open "Здравствуйте, меня зовут…").
    if msg.reaction or (msg.story_reply and not (msg.text or "").strip()):
        label = f"[реакция {msg.reaction}]" if msg.reaction else "[упоминание в сторис]"
        await save_message(session, user.id, "user", label)
        if not user.bot_paused:
            if msg.reaction:
                custom = (await get_setting(session, "reaction_reply") or "").strip()
                thanks = custom or _REACTION_THANKS
            else:
                thanks = _STORY_MENTION_THANKS
            mid = await messenger.send_text(user.external_id, thanks)
            ack = await save_message(session, user.id, "assistant", thanks)
            ack.status = "sent"
            if mid:
                ack.channel_mid = str(mid)
        return

    # Inbound voice note → download the bytes ONCE and persist a playable copy, so
    # operators can replay it in «Диалоги» regardless of whether transcription
    # succeeds. The same bytes are reused for speech-to-text below (no re-download).
    audio_bytes = None
    audio_media = None  # {"url", "kind"} once saved, else None
    if msg.has_audio:
        try:
            audio_bytes = await messenger.download_audio(msg.audio_ref or {})
        except Exception:  # noqa: BLE001 — never block the message on a media hiccup
            logger.warning("Inbound audio download failed for %s", user.id)
        if audio_bytes:
            try:
                audio_media = save_inbound_audio(audio_bytes)
            except OSError:
                logger.warning("Could not store inbound audio for %s", user.id)

    # Notify the operator's device (Web Push) about the incoming — even if the app
    # is closed. Best-effort: no push subscriptions → no-op.
    preview = msg.text or (
        "🎤 голосовое" if msg.has_audio
        else "📎 документ" if msg.has_document
        else "🖼 изображение" if msg.has_image
        else ""
    )
    try:
        await send_push(
            session,
            f"Сообщение от {user.full_name}",
            preview[:140],
            url=f"/admin/dialogs?u={user.id}",
        )
    except Exception:  # noqa: BLE001
        logger.debug("Push notify skipped")
    # Native app push (Expo → APNs/FCM). No registered devices → silent no-op.
    try:
        await expo_push.notify_new_message(
            session, name=user.full_name, preview=preview, user_id=user.id
        )
    except Exception:  # noqa: BLE001
        logger.debug("Expo push skipped")

    # Operator takeover: the bot stays silent for this chat. Still record what the
    # client sent so it shows up in the «Диалоги» inbox for the human operator.
    if user.bot_paused:
        if msg.has_image or msg.has_document:
            # Keep the client's image/PDF PLAYABLE+visible in «Диалоги», not a bare label —
            # a receipt sent while the operator has taken over must not vanish.
            await _record_incoming_file(session, user, messenger, msg)
        else:
            recorded = msg.text or ("🎤 Голосовое" if msg.has_audio else "")
            if recorded:
                umsg = await save_message(
                    session, user.id, "user", recorded, channel_mid=msg.external_message_id
                )
                if audio_media:  # attach the playable voice note for the operator
                    umsg.media_url = audio_media["url"]
                    umsg.media_kind = "voice"
                # Still mirror to the Telegram inbox so the operator sees new messages there —
                # with the voice note itself, otherwise it arrives as an unplayable label.
                await inbox_svc.mirror_incoming(
                    session,
                    user,
                    recorded,
                    media_url=audio_media["url"] if audio_media else None,
                    media_kind="voice" if audio_media else None,
                )
        return

    vcfg = await voice.voice_config(session)
    was_voice = False

    # A voice/audio message → transcribe it, then treat the text as the message.
    if msg.has_audio:

        async def _save_voice_only() -> None:
            """Record the voice note itself (playable) even when we can't turn it into
            text, so it's still visible in «Диалоги» and the operator can listen."""
            vmsg = await save_message(
                session, user.id, "user", "🎤 Голосовое", channel_mid=msg.external_message_id
            )
            if audio_media:
                vmsg.media_url = audio_media["url"]
                vmsg.media_kind = "voice"
            await inbox_svc.mirror_incoming(
                session,
                user,
                "🎤 Голосовое",
                media_url=audio_media["url"] if audio_media else None,
                media_kind="voice" if audio_media else None,
            )

        if not voice.is_enabled(vcfg):
            await _save_voice_only()
            await messenger.send_text(user.external_id, _VOICE_DISABLED)
            return
        transcribed = (
            await voice.transcribe(audio_bytes, filename="voice.ogg", cfg=vcfg)
            if audio_bytes
            else None
        )
        if not transcribed:
            await _save_voice_only()
            await messenger.send_text(user.external_id, _VOICE_UNCLEAR)
            return
        msg.text = transcribed
        was_voice = True

    # An image/PDF while an order is awaiting payment MIGHT be a payment receipt — but only
    # if it actually looks like one. A selfie / random photo must NOT be taken as proof.
    if msg.has_image or msg.has_document:
        rec = await _record_incoming_file(session, user, messenger, msg)  # → «Диалоги» + mirror
        order = await orders_svc.get_active_manual_order(session, user.id)
        if order is not None:
            # A PDF can't go through the image vision model → skip the check and route it to
            # the operator (is_receipt unknown). For an image, verify it's a real receipt +
            # read the amount; is_receipt None (LLM off/error) also degrades to operator review
            # so a genuine receipt is NEVER lost. The amount is compared in the operator note.
            analysis = (
                vision.ReceiptAnalysis(is_receipt=None, amount=None)
                if rec["is_pdf"]
                else await vision.analyze_receipt(session, rec["bytes"] or b"")
            )
            if analysis.is_receipt is False:
                await messenger.send_text(
                    user.external_id,
                    "Это не похоже на чек об оплате 🙈 Пришлите, пожалуйста, квитанцию или "
                    "скриншот успешного перевода — и я сразу передам её на проверку.",
                )
                return
            payment = await orders_svc.attach_receipt(session, order, str(rec["ref"]))
            await notify_manual_receipt(session, order, payment, receipt_amount=analysis.amount)
            await messenger.send_text(
                user.external_id,
                "Спасибо! Чек получен и отправлен на проверку ⏳ Как подтвердят — пришлём доступ.",
            )
            return

    if msg.text:
        # Mirror the client's message into the operator topic. For a transcribed voice note
        # the recording rides along with the transcript as its caption: the operator can
        # both read it fast and hear the tone/accent the transcript flattens out.
        await inbox_svc.mirror_incoming(
            session,
            user,
            msg.text,
            media_url=audio_media["url"] if audio_media else None,
            media_kind="voice" if audio_media else None,
        )
        reply_voice = voice.should_speak(vcfg, was_voice=was_voice, user_text=msg.text)
        # The platform id of this inbound message: drives WhatsApp's typing indicator and
        # is stored as the turn's channel_mid so an operator can react to it later.
        mid = msg.external_message_id
        dcfg = await get_setting(session, "debounce") or {}
        # Debounce a burst of fragmented one-word messages into ONE reply. Skipped for
        # voice (already a complete thought) and story replies (need their own directive).
        if dcfg.get("enabled", True) and not was_voice and not reply_voice and not msg.story_reply:
            await save_message(session, user.id, "user", msg.text, channel_mid=mid)
            await _maybe_opt_out(session, user, msg.text)
            try:
                await messenger.send_typing(user.external_id, message_id=mid)
            except Exception:  # noqa: BLE001
                pass
            debounce.schedule(
                user.id,
                delay=float(dcfg.get("seconds", 8) or 8),
                channel=msg.channel,
                incoming_message_id=mid,
            )
            return
        await ai_manager.handle_text(
            session,
            user=user,
            text=msg.text,
            messenger=messenger,
            tg_bot=None,
            reply_voice=reply_voice,
            situation_note=_STORY_SITUATION if msg.story_reply else None,
            incoming_message_id=mid,
            media_url=audio_media["url"] if audio_media else None,
            media_kind="voice" if audio_media else None,
        )


async def dispatch_comment(session: AsyncSession, comment: IncomingComment) -> None:
    """React to a new Instagram comment: a keyword FUNNEL (deterministic DM with a
    text/link) takes priority; otherwise the AI comment handler replies/DMs/hides spam
    per toggles. Loop-guarded so the bot never answers its own replies."""
    ig = ((await get_setting(session, "meta") or {}).get("instagram")) or {}

    # Loop guards: only top-level comments (our own replies carry a parent_id), and
    # never our own account's comments.
    if comment.is_reply:
        return
    if comment.from_id and comment.from_id == str(ig.get("ig_id") or ""):
        return
    if comment.from_username and comment.from_username == ig.get("username"):
        return

    messenger = await get_messenger(session, Channel.INSTAGRAM)

    # 1) Keyword funnel — if the comment contains a trigger word, DM the author the
    # configured text/link. Deterministic and independent of the general comments toggle.
    funnel = await comments_svc.match_funnel(session, comment.text)
    if funnel is not None:
        dm = (funnel.reply_value or "").strip()
        if dm:
            await messenger.private_reply(comment.comment_id, dm)
        if funnel.image_id:  # optional picture DM'd to the commenter (best-effort)
            asset = await session.get(MediaAsset, funnel.image_id)
            if asset is not None:
                try:
                    await messenger.private_reply_image(
                        comment.comment_id, public_url(asset.filename)
                    )
                except Exception:  # noqa: BLE001 — image is a bonus; the text DM already went
                    logger.warning("Funnel image DM failed for funnel %s", funnel.id)
        pub = (funnel.public_reply or "").strip()
        if pub:
            await messenger.reply_comment(comment.comment_id, pub)
        funnel.triggered_count += 1
        return

    # 2) General AI comment handling (gated by the «Комментарии» toggle).
    cfg = await get_setting(session, "comments") or {}
    if not cfg.get("enabled", True):
        return
    # Read the post's caption AND the whole comment thread so the bot answers in context
    # (a featured student is a student, not a teacher) and doesn't repeat what was already
    # said. Best-effort: any fetch failure just means less context, never a blocked reply.
    caption, thread = None, []
    if comment.media_id:
        try:
            caption = await messenger.fetch_media_caption(comment.media_id)
        except Exception:  # noqa: BLE001 — context is a nicety, never block the reply
            caption = None
        try:
            thread = await messenger.fetch_comments(comment.media_id)
        except Exception:  # noqa: BLE001
            thread = []
    action = await comments_svc.decide(session, comment.text, post_caption=caption, thread=thread)

    if action.get("is_spam"):
        if cfg.get("hide_spam", True):
            await messenger.hide_comment(comment.comment_id, True)
        return

    reply = action.get("reply") or ""
    if reply and cfg.get("reply_enabled", True):
        await messenger.reply_comment(comment.comment_id, reply)
    dm = action.get("dm") or ""
    if dm and cfg.get("dm_enabled", True):
        await messenger.private_reply(comment.comment_id, dm)
