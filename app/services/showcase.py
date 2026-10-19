"""Просят фото — показываем портфолио и каталог. Без участия модели.

Правило в промпте эту задачу не решило: разбор суток показал, что на пять
одинаковых просьб «пришлите фото» бот ответил по-разному — один раз портфолио,
один раз ссылкой на Instagram, а дважды не отправил ничего.
Просьба в промпте остаётся просьбой, поэтому решение принимает код:
распознали запрос — отправили полный набор материалов.

Отличать «покажите ваши фото» от «я пришлю своё фото» приходится явно: клиенты
часто предлагают свой снимок, и принять это за просьбу о каталоге значит
завалить человека ссылками в ответ на его же предложение.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.services.documents import search_documents
from app.services.media import public_url
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

SHOWCASE_KEY = "showcase"

# Ссылки у каждой компании свои — задаются настройкой «showcase» в админке.
# Пустая ссылка в сообщение не попадает.
DEFAULTS: dict = {
    "objects_url": "",
    "catalog_url": "",
    "document_query": "портфолио",
}

# Просьба показать: «фото», «фотки», «каталог», «объекты», «примеры», плюс
# казахское «сурет».
_ASK_RE = re.compile(
    r"(фото|фотк|фотограф|снимк|картинк|сурет|"
    r"каталог|прайс\s*с\s*фото|"
    r"объект|обьект|готовы[ех]\s+дом|примеры?\s+(?:работ|домов|фасад)|"
    r"портфоли|ваши\s+работы|как\s+выглядит|"
    # По-казахски, в том числе русскими буквами: «түрін жіберіңізші» (пришлите, как
    # выглядит), «көрсетіңізші» (покажите) — клиентке дважды ушла ссылка вместо фото.
    r"т[үу]р[іи]н\w*\s+ж[іи]бер|к[өо]рсет[іи]ң)",
    re.IGNORECASE,
)

# А это НЕ просьба: человек предлагает собственный снимок.
_OWN_PHOTO_RE = re.compile(
    r"(я\s+(?:могу\s+)?(?:отправ|скин|вышл|пришл|скид)|"
    r"(?:могу|хочу)\s+(?:отправить|скинуть|прислать|выслать)|"
    r"отправл[юя]|скину|пришлю|вышлю|высылаю|скидываю|"
    r"мо[её]\s+фото|фото\s+мо[ей][гйо]|своего\s+дома|мой\s+дом)",
    re.IGNORECASE,
)


def is_photo_request(text: str | None) -> bool:
    """Просит ли клиент показать фото, объекты или каталог."""
    body = (text or "").strip()
    if not body or _OWN_PHOTO_RE.search(body):
        return False
    return bool(_ASK_RE.search(body))


async def showcase_config(session: AsyncSession) -> dict:
    """Ссылки витрины: сохранённые поверх значений по умолчанию."""
    saved = await get_setting(session, SHOWCASE_KEY) or {}
    cfg = dict(DEFAULTS)
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if v})
    return cfg


def showcase_text(cfg: dict) -> str:
    """Сообщение со ссылками — или пусто, если ни одной ссылки не задано."""
    lines = []
    if cfg.get("objects_url"):
        lines.append(f"📸 Наши работы: {cfg['objects_url']}")
    if cfg.get("catalog_url"):
        lines.append(f"🛒 Каталог с фото и ценами: {cfg['catalog_url']}")
    if not lines:
        return ""
    return "Показываю наши работы и каталог 👇\n\n" + "\n".join(lines)


async def send_showcase(session: AsyncSession, user: User, messenger) -> bool:
    """Отправить клиенту портфолио и ссылки. True, если хоть что-то ушло.

    Документ отправляем первым: его листают прямо в мессенджере, не переходя
    на сайт. Если портфолио в библиотеке не нашлось, ссылки всё равно уйдут —
    остаться совсем без ответа на просьбу о фото хуже всего.
    """
    from app.sales.conversation import record_sent_media, record_sent_text, window_open

    if not await window_open(session, user):
        # Вне 24-часового окна Meta принимает запрос (200), но не доставляет —
        # снаружи это выглядело как успешная отправка. Честно говорим «нет».
        logger.warning("showcase: окно клиента %s закрыто, портфолио не отправлено", user.id)
        return False

    cfg = await showcase_config(session)
    sent_anything = False

    found = await search_documents(session, cfg["document_query"], limit=1)
    doc = found[0] if found else None
    if doc is not None:
        caption = "Вот наши работы — посмотрите примеры."
        try:
            await messenger.send_media(
                user.external_id,
                url=public_url(doc.filename),
                mime=doc.mime,
                kind=doc.kind,
                filename=doc.title,
                caption=caption,
            )
            await record_sent_media(
                session,
                user,
                title=doc.title,
                word="документ",
                url=public_url(doc.filename),
                kind=doc.kind or "document",
                caption=caption,
            )
            sent_anything = True
        except Exception:  # noqa: BLE001 — ссылки ниже уйдут в любом случае
            logger.warning("showcase: документ «%s» не отправился", doc.title)
    else:
        logger.warning("showcase: портфолио не найдено в библиотеке документов")

    text = showcase_text(cfg)
    if not text:
        return sent_anything
    try:
        await messenger.send_text(user.external_id, text)
        await record_sent_text(session, user, text)
        sent_anything = True
    except Exception:  # noqa: BLE001
        logger.exception("showcase: ссылки не отправились пользователю %s", user.id)

    return sent_anything
