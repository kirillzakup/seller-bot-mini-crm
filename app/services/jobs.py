"""Scheduled background jobs (run by APScheduler).

Each opens its own DB session and reuses existing building blocks:
- expire_subscriptions  → FulfillmentProvider.revoke (kick from channel)
- nudge_abandoned_orders → Messenger.send_text + /pay/{code} link
- reconcile_pending_payments → PaymentProvider.poll_status → confirm_payment

All sends/revokes are best-effort (wrapped) so one failure never aborts the batch.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.channels.meta import GRAPH_IG, get_reengage_template
from app.channels.registry import get_messenger
from app.config import get_settings
from app.db.base import AsyncSessionLocal
from app.db.models import (
    AccessGrant,
    Event,
    Followup,
    Lead,
    MediaAsset,
    Message,
    Order,
    PaymentMethod,
    Product,
    User,
)
from app.db.models.enums import Channel, LeadStatus, OrderStatus, PaymentStatus
from app.fulfillment.registry import build_fulfillment
from app.payments.registry import build_provider
from app.sales import client_profile
from app.sales.conversation import save_message, window_open
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.sales.prompts import DEFAULT_BIRTHDAY_TEXT, DEFAULT_SILENCE_TEXTS
from app.services import learning
from app.services import orders as orders_svc
from app.services.events import log_event
from app.services.media import is_picture, public_url, search_media
from app.services.notify import send_operator_message
from app.services.settings_service import get_local_tz, get_setting, set_setting

logger = logging.getLogger(__name__)


def _recipient_unreachable(exc: Exception) -> bool:
    """True when a send failed permanently — the recipient blocked the bot, deactivated,
    or the chat no longer exists. Such a lead can never be reached again, so retrying it
    every cycle only spams the logs; we mark it blocked so it drops out of outreach."""
    from aiogram.exceptions import TelegramForbiddenError

    if isinstance(exc, TelegramForbiddenError):
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in ("bot was blocked", "user is deactivated", "chat not found", "user is deleted")
    )

NUDGE_AFTER_HOURS = 24  # remind if unpaid for this long
NUDGE_WINDOW_DAYS = 7  # …but not older than this (ignore stale drafts)


def _tg_bot():
    from app.bot.runner import get_bot

    return get_bot()


async def expire_subscriptions() -> int:
    """Revoke access for grants whose subscription window has elapsed."""
    revoked = 0
    async with AsyncSessionLocal() as session:
        now = datetime.now(UTC)
        grants = (
            (
                await session.execute(
                    select(AccessGrant).where(
                        AccessGrant.expires_at.is_not(None),
                        AccessGrant.expires_at < now,
                        AccessGrant.revoked.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

        for g in grants:
            product = await session.get(Product, g.product_id)
            user = await session.get(User, g.user_id)
            if product is not None and user is not None:
                try:
                    await build_fulfillment(product).revoke(
                        tg_bot=_tg_bot(), channel_id=g.channel_id, user=user
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Revoke failed for grant %s", g.id)

            g.revoked = True
            g.revoked_at = now
            await log_event(
                session, "access_revoked", user_id=g.user_id, data={"order_id": g.order_id}
            )

            if user is not None:
                try:
                    messenger = await get_messenger(session, user.channel)
                    await messenger.send_text(
                        user.external_id,
                        "Срок доступа истёк. Чтобы продлить — оформите заказ заново 🙏",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("Expiry notice failed for user %s", g.user_id)
            revoked += 1

        await session.commit()
    if revoked:
        logger.info("Expired %s subscription(s)", revoked)
    return revoked


async def nudge_abandoned_orders() -> int:
    """Remind users who created an order but haven't paid."""
    sent = 0
    async with AsyncSessionLocal() as session:
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=NUDGE_AFTER_HOURS)
        floor = now - timedelta(days=NUDGE_WINDOW_DAYS)
        orders = (
            (
                await session.execute(
                    select(Order).where(
                        Order.status == OrderStatus.AWAITING_PAYMENT,
                        Order.created_at < cutoff,
                        Order.created_at > floor,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not orders:
            return 0

        already = {
            e.data.get("code")
            for e in (
                await session.execute(select(Event).where(Event.type == "order_nudged"))
            ).scalars()
        }
        base = get_settings().base_url.rstrip("/")

        for o in orders:
            if o.code in already:
                continue
            user = await session.get(User, o.user_id)
            if user is None:
                continue
            try:
                messenger = await get_messenger(session, user.channel)
                # Окно WhatsApp закрыто → свободный текст отклонят. НЕ отмечаем заказ
                # как дожатый: без отметки он попадёт в следующий цикл и уйдёт, как
                # только клиент напишет сам. Иначе напоминание об оплате исчезало.
                if not await window_open(session, user, now):
                    if user.channel == Channel.WHATSAPP:
                        await _reengage(session, messenger, user, now)
                    continue
                mid = await messenger.send_text(
                    user.external_id,
                    f"Вы не завершили оплату заказа {o.code}. Ссылка: {base}/pay/{o.code}",
                )
            except Exception:  # noqa: BLE001
                logger.warning("Nudge failed for order %s", o.code)
                continue
            if not mid and user.channel in (Channel.WHATSAPP, Channel.INSTAGRAM):
                continue  # канал не принял — попробуем в следующем цикле
            await _record_outgoing(
                session,
                user,
                f"Вы не завершили оплату заказа {o.code}. Ссылка: {base}/pay/{o.code}",
            )
            await log_event(session, "order_nudged", user_id=o.user_id, data={"code": o.code})
            sent += 1

        await session.commit()
    if sent:
        logger.info("Nudged %s abandoned order(s)", sent)
    return sent


_FOLLOWUP_RETRY_HOURS = 6  # как часто перепроверять, открылось ли окно
_FOLLOWUP_TTL_DAYS = 14  # после этого дожим уже неактуален — закрываем


def _postpone(f: Followup, now: datetime) -> None:
    """Сдвинуть дожим на следующую проверку, не помечая отправленным."""
    f.due_at = now + timedelta(hours=_FOLLOWUP_RETRY_HOURS)


def _expired(f: Followup, now: datetime) -> bool:
    created = _as_utc(f.created_at) if f.created_at else now
    return (now - created) > timedelta(days=_FOLLOWUP_TTL_DAYS)


async def _record_outgoing(
    session,
    user: User,
    text: str,
    *,
    media_url: str | None = None,
    media_kind: str | None = None,
) -> None:
    """Записать инициированное ботом сообщение в «Диалоги» и зеркало Telegram.

    Дожимы, напоминания об оплате и поздравления уходили клиенту напрямую и нигде
    не отражались: оператор открывал диалог и не понимал, что бот уже писал — а
    модель на следующем ходу не видела собственного напоминания в истории.
    """
    from app.services import inbox as inbox_svc  # lazy: avoids an import cycle

    row = await save_message(session, user.id, "assistant", text)
    row.status = "sent"
    if media_url:
        row.media_url = media_url
        row.media_kind = media_kind
    try:
        await inbox_svc.mirror_outgoing(
            session, user, text, who="bot", media_url=media_url, media_kind=media_kind
        )
    except Exception:  # noqa: BLE001 — зеркало не должно ронять рассылку
        logger.warning("job mirror failed for user %s", user.id)


async def _reengage(session, messenger, user: User, now: datetime) -> bool:
    """Открыть закрытое окно одобренным шаблоном — один раз на лида.

    Возвращает True, если шаблон реально ушёл. Повторно не тратимся: клиент,
    который не ответил на первый шаблон, не ответит и на пятый, а каждый стоит денег.
    """
    tmpl = await get_reengage_template(session)
    if not tmpl or user.reengaged_at is not None:
        return False
    mid = await messenger.send_template(
        user.external_id, name=tmpl["name"], language=tmpl["language"]
    )
    if not mid:
        return False
    user.reengaged_at = now
    await save_message(session, user.id, "assistant", tmpl["text"] or f"[шаблон {tmpl['name']}]")
    await log_event(session, "reengage_template_sent", user_id=user.id, data={"name": tmpl["name"]})
    return True


async def send_due_followups() -> int:
    """Deliver scheduled follow-ups whose due time has passed — once each."""
    sent = 0
    async with AsyncSessionLocal() as session:
        now = datetime.now(UTC)
        rows = (
            (
                await session.execute(
                    select(Followup).where(Followup.sent.is_(False), Followup.due_at < now)
                )
            )
            .scalars()
            .all()
        )
        for f in rows:
            user = await session.get(User, f.user_id)
            if user is None or user.is_blocked:
                f.sent, f.sent_at = True, now
                continue
            try:
                messenger = await get_messenger(session, user.channel)
                # «Подумаю — напишите через неделю» по определению срабатывает позже суток,
                # а к тому моменту окно WhatsApp закрыто и Meta отклоняет свободный текст
                # (131047). Раньше дожим всё равно помечался отправленным и пропадал —
                # терялись самые тёплые лиды. Теперь он ждёт, пока окно откроется.
                if not await window_open(session, user, now):
                    # Шаблоны есть только у WhatsApp; в Instagram просто ждём ответа клиента.
                    if user.channel == Channel.WHATSAPP and await _reengage(
                        session, messenger, user, now
                    ):
                        # Шаблон отправлен: окно откроется, когда клиент ответит —
                        # до тех пор придерживаем сам дожим.
                        _postpone(f, now)
                        continue
                    if _expired(f, now):
                        f.sent, f.sent_at = True, now
                        await log_event(
                            session, "followup_expired", user_id=f.user_id, data={"kind": f.kind}
                        )
                        logger.info("Follow-up %s expired: 24h window never reopened", f.id)
                    else:
                        _postpone(f, now)
                    continue
                mid = await messenger.send_text(user.external_id, f.message)
            except Exception:  # noqa: BLE001
                logger.warning("Follow-up send failed for user %s", f.user_id)
                continue
            if not mid and user.channel in (Channel.WHATSAPP, Channel.INSTAGRAM):
                # Канал ответил отказом (окно закрылось между проверкой и отправкой) —
                # не выдаём это за доставку.
                _postpone(f, now)
                continue
            f.sent, f.sent_at = True, now
            await _record_outgoing(session, user, f.message)
            await log_event(session, "followup_sent", user_id=f.user_id, data={"kind": f.kind})
            sent += 1
        await session.commit()
    if sent:
        logger.info("Sent %s follow-up(s)", sent)
    return sent


def _as_utc(dt: datetime) -> datetime:
    """SQLite may return naive datetimes; treat them as UTC for safe arithmetic."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_hhmm(at: str) -> tuple[int, int]:
    """Parse a 'HH:MM' send time; fall back to 10:00 on anything malformed."""
    try:
        hh, mm = str(at).split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, TypeError):
        pass
    return 10, 0


def _step_due(last_seen_utc: datetime, step: dict, tz: timezone) -> datetime:
    """UTC instant reminder ``step`` should fire: ``day`` days after the client's
    last activity, at local time ``at`` (HH:MM). Rolled forward one day if that
    instant isn't strictly after the last activity (so it never fires 'in the past')."""
    last_local = last_seen_utc.astimezone(tz)
    hour, minute = _parse_hhmm(step.get("at", "10:00"))
    try:
        day = max(0, int(step.get("day", 0) or 0))
    except (TypeError, ValueError):
        day = 0
    due_date = (last_local + timedelta(days=day)).date()
    due_local = datetime(due_date.year, due_date.month, due_date.day, hour, minute, tzinfo=tz)
    if due_local <= last_local:
        due_local += timedelta(days=1)
    return due_local.astimezone(UTC)


_NOT_A_NAME_RE = re.compile(
    r"компан|фирм|\bтоо\b|\bооо\b|\bип\b|магазин|склад|завод|"
    r"shop|store|доставк|строй",
    re.I,
)


def _client_name(user) -> str:
    """Имя для обращения. В WhatsApp Business в профиле часто стоит название
    фирмы — обращаться «Здравствуйте, ТОО Стройснаб» нельзя."""
    raw = (getattr(user, "first_name", None) or "").strip()
    if not raw or len(raw) > 24 or "|" in raw or _NOT_A_NAME_RE.search(raw):
        return ""
    if len(raw.split()) > 2 or any(ch.isdigit() for ch in raw):
        return ""
    return raw


NUDGE_SYSTEM = """Ты — менеджер отдела продаж компании.

Клиент интересовался товаром и перестал отвечать. Напиши ОДНО короткое сообщение, чтобы вернуть его в диалог.

# Железные правила

1. Не выдумывай факты. Никаких акций, скидок, «осталось N штук», «цена вырастет», отзывов и примеров, которых нет в переписке. Нет факта — работай тоном, а не выдуманным поводом.
2. Никакого давления и срочности. Не пиши про оплату, счёт и задолженность.
3. Одно-два коротких предложения. Максимум один эмодзи. Это WhatsApp, а не письмо.
4. Обращайся по имени, только если оно передано. Если имени нет — просто начни с сути, без «здравствуйте, уважаемый клиент».
5. Заканчивай лёгким вопросом, на который можно ответить одним словом.
6. Про «вопрос закрыт» пиши ТОЛЬКО в третьем напоминании. В первом и втором этой фразы быть не должно — иначе выглядит, будто ты сама выпроваживаешь клиента.
7. Юмор — приправа, а не блюдо. Тёплая улыбка, лёгкая самоирония. Без пошлости, без «ну как, надумали?», без канцелярита.
8. Посмотри в переписке, что мы уже отправляли. Повторять заход, интонацию или шутку нельзя.

# Какой приём брать на этом шаге

Сейчас напоминание номер {step}.

**Первое напоминание — зацепись за конкретику.** Вспомни город, модель, объём, задачу из переписки и вернись именно к ним. Про «вопрос закрыт» не пиши.
«Ерлан, подборка под вашу задачу до сих пор у меня в закладках 🙂 Посчитать стоимость?»
«Айгуль, расчёт по вашему заказу готов и никуда не делся. Напомнить цифры?»

**Второе напоминание — смени угол: польза, выбор или самоирония.** Не повторяй первое. Про «вопрос закрыт» по-прежнему не пиши.
«Нурлан, отправить короткую памятку, как выбрать без ошибок?»
«Гульнара, вам как удобнее: скинуть каталог с ценами или сразу посчитать ваш вариант?»

**Третье напоминание — прощальное.** Самый сильный приём: ты закрываешь вопрос сама, без обид и уговоров. Здесь фраза про «вопрос закрыт» обязательна.
«Бахыт, закрываю ваш вопрос, чтобы не надоедать 🙂 Станет актуально — напишите, я на связи.»
«Динара, больше не буду напоминать о себе. Если вопрос вернётся — вы знаете, где меня найти. А если хотите, чтобы я совсем не писала, черкните «вопрос закрыт».»

# Что вернуть

Только текст сообщения. Без кавычек, подписи и пояснений."""


OPT_OUT_HINT = (
    "Если хотите, чтобы я больше не писала — просто ответьте «вопрос закрыт»."
)


def _ensure_opt_out(text: str) -> str:
    """Последнее напоминание обязано давать человеку способ отписаться.

    Модель эту фразу периодически теряет, а без неё цепочка выглядит как спам
    без выхода — поэтому дописываем гарантированно.
    """
    if "вопрос закрыт" in (text or "").lower():
        return text
    return (text or "").rstrip() + chr(10) * 2 + OPT_OUT_HINT


async def _compose_nudge(
    session, user, step: int, fallback: str, llm_cfg: dict, *, last: bool = False
) -> str:
    """Живое напоминание под конкретный диалог; при любой осечке — заготовленный текст."""
    if not llm_cfg:
        return _ensure_opt_out(fallback) if last else fallback
    try:
        # Тон напоминаний правится из настроек, без пересборки образа.
        tpl = (await get_setting(session, "nudge_prompt") or "").strip() or NUDGE_SYSTEM
        history = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.user_id == user.id)
                    .order_by(Message.id.desc())
                    .limit(6)
                )
            ).scalars()
        )[::-1]
        lines = [
            ("клиент: " if m.role == "user" else "мы: ") + (m.content or "")[:200]
            for m in history
            if m.role in ("user", "assistant", "operator")
        ]
        name = _client_name(user)
        ctx = "\n".join(lines) or "переписки почти не было — клиент только пришёл"
        msg = await chat_completion(
            messages=[
                {"role": "system", "content": tpl.format(step=step + 1)},
                {
                    "role": "user",
                    "content": (
                        "Имя клиента: " + (name or "неизвестно") + "\n"
                        "Последние сообщения:\n" + ctx + "\n\n"
                        "Напиши напоминание."
                    ),
                },
            ],
            tools=None,
            config=llm_cfg,
        )
        text = (msg.get("content") or "").strip().strip('"')
        if 15 <= len(text) <= 600:
            return _ensure_opt_out(text) if last else text
        logger.warning("Nudge text rejected (len=%s) for user %s", len(text), user.id)
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("Nudge compose failed for user %s: %s", user.id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("Nudge compose crashed for user %s: %s", user.id, e)
    return _ensure_opt_out(fallback) if last else fallback


async def nudge_silent_leads() -> int:
    """Re-engage quiet leads with a configurable SEQUENCE of reminders.

    Reminder ``n`` (0-based) fires at ``last_seen + silence_hours + n*interval``.
    ``User.reminders_sent`` tracks progress and resets to 0 when the lead replies.
    """
    sent = 0
    async with AsyncSessionLocal() as session:
        cfg = await get_setting(session, "followup") or {}
        if not cfg.get("silence_enabled", True):
            return 0
        tz = await get_local_tz(session)

        # Preferred: a sequence of steps each with an explicit send time
        # ({day, at, text}). Legacy fallback: hours-based first-delay + interval.
        steps = [s for s in (cfg.get("silence_steps") or []) if str(s.get("text", "")).strip()]
        use_steps = bool(steps)
        step_images: list = []
        if use_steps:
            texts = [str(s["text"]).strip() for s in steps]
            step_images = [s.get("image_id") for s in steps]
        else:
            raw = cfg.get("silence_texts") or DEFAULT_SILENCE_TEXTS
            texts = [str(t).strip() for t in raw if str(t).strip()]
            first_delay = float(cfg.get("silence_hours", 3) or 3)
            interval = float(cfg.get("silence_interval_hours", 24) or 24)
        if not texts:
            return 0
        now = datetime.now(UTC)
        floor = now - timedelta(days=30)  # ignore leads quiet longer than this

        leads = (
            (
                await session.execute(
                    select(User).where(
                        User.last_seen.is_not(None),
                        User.last_seen > floor,
                        User.is_blocked.is_(False),
                        User.reminders_enabled.is_(True),  # per-client «дожим» opt-out
                    )
                )
            )
            .scalars()
            .all()
        )
        if not leads:
            return 0

        paid = set(
            (
                await session.execute(
                    select(Order.user_id).where(
                        Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLED])
                    )
                )
            ).scalars()
        )
        pending = {
            f.user_id
            for f in (
                await session.execute(select(Followup).where(Followup.sent.is_(False)))
            ).scalars()
        }
        # Attach a "silence"/"ждун" meme to reminders when one is in the library —
        # rotate through matches by step so each nudge looks different.
        silence_memes = [
            m
            for m in await search_media(
                session, "тишина игнор молчит не отвечает подумаю ждун ждём", limit=5
            )
            if is_picture(m)  # ссылки-ассеты без файла уходили как битое фото «/media/»
        ]
        llm_cfg = await get_setting(session, "llm") or {}
        waiting = set(
            (
                await session.execute(
                    select(Lead.user_id).where(Lead.status == LeadStatus.WAITING)
                )
            ).scalars()
        )

        for u in leads:
            n = u.reminders_sent or 0
            if n >= len(texts) or u.id in paid or u.id in pending:
                continue
            # «Я сам позвоню», «уже решил», «купил в другом месте» — портрет клиента это
            # помнит. Дожим после таких слов только раздражает и рушит доверие.
            if not client_profile.nudges_allowed(u):
                continue
            # Клиент «в ожидании»: менеджер сам обещал написать в названный день —
            # автодожим поверх этой договорённости только сбивает.
            if u.id in waiting:
                continue
            last = _as_utc(u.last_seen)
            if use_steps:
                due = _step_due(last, steps[n], tz)
            else:
                due = last + timedelta(hours=first_delay + n * interval)
            if now < due:
                continue
            try:
                messenger = await get_messenger(session, u.channel)
                if not await window_open(session, u, now):
                    # 24h window closed → a free-form nudge would be rejected. Re-open it
                    # with an approved template, but only ONCE per lead (the first closed
                    # step) so we never spend repeatedly on someone who stays silent.
                    # В Instagram шаблонов нет — молча ждём, пока клиент напишет сам.
                    if u.channel != Channel.WHATSAPP or not await _reengage(
                        session, messenger, u, now
                    ):
                        continue  # нет шаблона / уже использовали / отправка не прошла
                else:
                    # Prefer the image the operator pinned to this step; else auto-pick a
                    # «тишина»/«ждун» meme; else plain text. Text rides along as caption.
                    img_url = None
                    chosen = step_images[n] if n < len(step_images) else None
                    if chosen:
                        asset = await session.get(MediaAsset, int(chosen))
                        if asset is not None:
                            img_url = public_url(asset.filename)
                    if img_url is None and silence_memes:
                        img_url = public_url(silence_memes[n % len(silence_memes)].filename)
                    body = await _compose_nudge(
                        session, u, n, texts[n], llm_cfg, last=(n + 1 >= len(texts))
                    )
                    if img_url is not None:
                        await messenger.send_image(u.external_id, img_url, body)
                        await _record_outgoing(
                            session, u, body, media_url=img_url, media_kind="photo"
                        )
                    else:
                        await messenger.send_text(u.external_id, body)
                        await _record_outgoing(session, u, body)
            except Exception as e:  # noqa: BLE001
                if _recipient_unreachable(e):
                    # Blocked the bot / deactivated → mark blocked so we stop retrying it
                    # every cycle (the leads query filters is_blocked out). Cleared if they
                    # ever message us again (services.users).
                    u.is_blocked = True
                    await log_event(
                        session, "lead_unreachable", user_id=u.id, data={"reason": str(e)[:200]}
                    )
                    logger.info("Lead %s marked blocked (unreachable): %s", u.id, e)
                else:
                    logger.warning("Silence reminder failed for user %s: %s", u.id, e)
                continue
            u.reminders_sent = n + 1
            await log_event(session, "silence_nudged", user_id=u.id, data={"step": n + 1})
            sent += 1
        await session.commit()
    if sent:
        logger.info("Sent %s silence reminder(s)", sent)
    return sent


async def greet_birthdays() -> int:
    """Congratulate clients whose birthday is today (once per year, if opted in)."""
    sent = 0
    async with AsyncSessionLocal() as session:
        cfg = await get_setting(session, "loyalty") or {}
        if not cfg.get("birthday_enabled", True):
            return 0
        text = (cfg.get("birthday_text") or DEFAULT_BIRTHDAY_TEXT).strip() or DEFAULT_BIRTHDAY_TEXT
        today = datetime.now(await get_local_tz(session)).date()

        users = (
            (
                await session.execute(
                    select(User).where(
                        User.birthday.is_not(None),
                        User.birthday_greet.is_(True),
                        User.is_blocked.is_(False),
                        User.promos_enabled.is_(True),  # per-client promos opt-out
                    )
                )
            )
            .scalars()
            .all()
        )
        greeted = {
            (e.user_id, (e.data or {}).get("year"))
            for e in (
                await session.execute(select(Event).where(Event.type == "birthday_greeted"))
            ).scalars()
        }
        for u in users:
            if u.birthday.month != today.month or u.birthday.day != today.day:
                continue
            if (u.id, today.year) in greeted:
                continue
            try:
                messenger = await get_messenger(session, u.channel)
                await messenger.send_text(u.external_id, text)
            except Exception:  # noqa: BLE001
                logger.warning("Birthday greet failed for user %s", u.id)
                continue
            await _record_outgoing(session, u, text)
            await log_event(session, "birthday_greeted", user_id=u.id, data={"year": today.year})
            sent += 1
        await session.commit()
    if sent:
        logger.info("Greeted %s birthday(s)", sent)
    return sent


async def reconcile_pending_payments() -> int:
    """Poll gateway status for orders awaiting payment; confirm any that paid."""
    confirmed = 0
    async with AsyncSessionLocal() as session:
        orders = (
            (
                await session.execute(
                    select(Order).where(
                        Order.status == OrderStatus.AWAITING_PAYMENT,
                        Order.provider_type.is_not(None),
                        Order.external_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        for o in orders:
            method = (
                (
                    await session.execute(
                        select(PaymentMethod).where(
                            PaymentMethod.type == o.provider_type,
                            PaymentMethod.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if method is None:
                continue
            try:
                status = await build_provider(method).poll_status(o.external_id)
            except Exception:  # noqa: BLE001
                logger.exception("poll_status failed for order %s", o.code)
                continue
            if status != PaymentStatus.CONFIRMED:
                continue
            user = await session.get(User, o.user_id)
            if user is None:
                continue
            try:
                messenger = await get_messenger(session, user.channel)
                await orders_svc.confirm_payment(
                    session, o, by="reconcile", messenger=messenger, tg_bot=_tg_bot()
                )
                confirmed += 1
            except Exception:  # noqa: BLE001
                logger.exception("Reconcile confirm failed for order %s", o.code)

        await session.commit()
    if confirmed:
        logger.info("Reconciled %s payment(s)", confirmed)
    return confirmed


IG_REFRESH_WINDOW = timedelta(days=10)  # refresh the IG token within this of expiry


async def refresh_instagram_token() -> bool:
    """Extend the long-lived Instagram token before it expires (~60 days).

    Instagram User tokens die after 60 days. ``graph.instagram.com``'s
    ``refresh_access_token`` issues a fresh 60-day token (no app secret needed).
    We refresh only when within IG_REFRESH_WINDOW of expiry — and also on the first
    run when the expiry isn't known yet, which stamps it for subsequent runs.
    """
    async with AsyncSessionLocal() as session:
        meta = await get_setting(session, "meta") or {}
        ig = meta.get("instagram") or {}
        token = ig.get("access_token")
        if not ig.get("enabled") or not token:
            return False

        now = datetime.now(UTC)
        expires_raw = ig.get("token_expires_at")
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at - now > IG_REFRESH_WINDOW:
                    return False  # still fresh — nothing to do
            except ValueError:
                pass  # unparseable → refresh to re-establish it

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.get(
                    f"{GRAPH_IG}/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": token},
                )
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Instagram token refresh failed: %s", e)
            return False

        new_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not new_token or not expires_in:
            logger.warning("Instagram token refresh returned no token: %s", data)
            return False

        ig["access_token"] = new_token
        ig["token_expires_at"] = (now + timedelta(seconds=int(expires_in))).isoformat()
        meta["instagram"] = ig
        await set_setting(session, "meta", meta)
        await session.commit()
        logger.info("Instagram token refreshed; valid until %s", ig["token_expires_at"])
        return True


AUTO_ANALYZE_DEFAULT_DAYS = 7  # how often auto script analysis runs, when enabled


async def auto_analyze_script() -> bool:
    """Unattended sales-script improvement — the «Обучение» button on a schedule.

    Only when enabled, at most once per configured interval, and never piling up
    (skips while a proposal already awaits review). Applying a proposal stays a
    manual, owner-approved click — this only prepares it and pings the owner.
    """
    async with AsyncSessionLocal() as session:
        cfg = await get_setting(session, "learning") or {}
        if not cfg.get("auto_analyze_enabled", False):
            return False

        now = datetime.now(UTC)
        days = float(cfg.get("auto_analyze_days") or AUTO_ANALYZE_DEFAULT_DAYS)
        last_raw = cfg.get("last_auto_analyze_at")
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if now - last < timedelta(days=days):
                    return False  # not due yet
            except ValueError:
                pass

        # Don't stack proposals — wait until the owner reviews the last one.
        if await learning.pending_script_proposals(session, limit=1):
            return False

        proposal, reason = await learning.analyze_successful_dialogs(session)
        # Stamp the run time regardless, so we don't re-run every tick with no data.
        cfg["last_auto_analyze_at"] = now.isoformat()
        await set_setting(session, "learning", cfg)
        await session.commit()

        if proposal is None:
            logger.info("Auto script analysis: nothing to propose (%s)", reason)
            return False

        logger.info(
            "Auto script analysis: proposal %s ready (%s dialogs)", proposal.id, proposal.based_on
        )
        try:
            await send_operator_message(
                session,
                "🧠 Бот подготовил предложение по улучшению скрипта продаж "
                f"(по {proposal.based_on} успешным диалогам). Загляните в админку → "
                "«🧠 Обучение», чтобы применить или отклонить.",
            )
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.debug("Auto-analyze owner notification skipped")
        return True



# Ежедневная сводка по заявкам в работе. Час берём из настроек, часовой пояс —
# рабочий (Казахстан, UTC+5). Проверяем час на каждом тике, а не вешаем cron с
# фиксированной зоной: так напоминание переживает перезапуск в 9:30 и подхватит
# смену часового пояса в настройках без правки кода.
LEADS_REMIND_HOUR_KEY = "leads_reminder_hour"
LEADS_REMIND_DATE_KEY = "leads_reminder_date"
LEADS_REMIND_HOUR = 9


async def remind_waiting_leads() -> int:
    """Клиент просил связаться позже — в названный менеджером срок карточка снова
    в теме заявок. Каждые несколько минут, чтобы «завтра в 14:00» значило 14:00."""
    from app.services.leads_reminders import repost_waiting_leads  # lazy, как сводка

    async with AsyncSessionLocal() as session:
        sent = await repost_waiting_leads(session)
        await session.commit()
        return sent


async def remind_leads_in_work() -> int:
    """Раз в сутки напомнить продавцам про заявки, которые висят в работе."""
    from app.services import leads_service
    from app.services.settings_service import get_local_tz, set_setting

    async with AsyncSessionLocal() as session:
        tz = await get_local_tz(session)
        now = datetime.now(tz)

        raw_hour = await get_setting(session, LEADS_REMIND_HOUR_KEY)
        try:
            hour = int(raw_hour) if raw_hour not in (None, "") else LEADS_REMIND_HOUR
        except (TypeError, ValueError):
            hour = LEADS_REMIND_HOUR
        if now.hour != hour:
            return 0

        today = now.date().isoformat()
        if await get_setting(session, LEADS_REMIND_DATE_KEY) == today:
            return 0  # сегодня уже напоминали

        sent = await leads_service.remind_leads_in_work(session)
        # Дату ставим в любом случае: если заявок в работе нет, повторять нечего.
        await set_setting(session, LEADS_REMIND_DATE_KEY, today)
        await session.commit()
        return sent
