"""Правила продажи, живущие в карточке товара.

Характер бота, скрипт продаж, голос и материалы задаются для КАЖДОГО товара
отдельно — в его карточке и больше нигде. Категория товаров к настройкам
отношения не имеет: она только группирует каталог и служит областью для кнопки
«применить к категории».

Главное правило: **пустое поле карточки = берётся общая настройка бота**. Голос
общий по умолчанию и меняется, только если явно задан в карточке.

Пока бот не понял, о каком товаре речь (клиент просто поздоровался), работают
общие настройки. Как только товар определился — включаются его правила.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaAsset, Message, Order, Product, ProductMedia
from app.db.models.enums import OrderStatus

_SCRIPT_HEAD = "Скрипт продаж — веди клиента по этим этапам (гибко, по-человечески, не роботом):\n"

# Сколько последних сообщений просматриваем в поисках упоминания товара.
_LOOKBACK = 12


async def active_product(session: AsyncSession, user_id: int) -> Product | None:
    """Товар, о котором идёт разговор, или None.

    Порядок надёжности: сначала оформленный заказ (клиент уже выбрал), затем
    упоминание названия в последних сообщениях. Ничего не нашли — это не ошибка,
    а нормальное начало диалога: работают общие настройки бота.
    """
    order = (
        await session.execute(
            select(Order)
            .where(Order.user_id == user_id, Order.status != OrderStatus.CANCELED)
            .order_by(Order.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if order is not None and order.product_id:
        product = await session.get(Product, order.product_id)
        if product is not None and product.is_active:
            return product

    products = list(
        (await session.execute(select(Product).where(Product.is_active.is_(True)))).scalars().all()
    )
    if not products:
        return None

    texts = list(
        (
            await session.execute(
                select(Message.content)
                .where(Message.user_id == user_id)
                .order_by(Message.id.desc())
                .limit(_LOOKBACK)
            )
        )
        .scalars()
        .all()
    )
    # Длинные названия проверяем первыми: «Гладкий Графит» точнее, чем «Гладкий».
    by_length = sorted(products, key=lambda p: -len(p.title or ""))
    for text in texts:  # свежие сообщения важнее старых
        low = (text or "").lower()
        if not low.strip():
            continue
        for product in by_length:
            title = (product.title or "").strip().lower()
            if title and title in low:
                return product
    return None


def persona_for(product: Product | None, fallback: str) -> str:
    """Характер бота: из карточки товара, иначе общий."""
    if product is not None and (product.persona_prompt or "").strip():
        return product.persona_prompt.strip()
    return fallback


def script_block(product: Product | None, fallback: str) -> str:
    """Блок скрипта продаж для системного промпта.

    Пустая строка, когда говорить нечего — чтобы не плодить заголовок без текста.
    """
    own = (product.sales_script or "").strip() if product is not None else ""
    if own:
        return _SCRIPT_HEAD + own
    text = (fallback or "").strip()
    return _SCRIPT_HEAD + text if text else ""


def voice_overrides(product: Product | None) -> dict:
    """Что переопределить в общих настройках голоса для этого товара.

    Пусто — звучит общий голос бота. Так и задумано: смена голоса посреди
    разговора читается как подмена собеседника, поэтому это осознанное действие
    владельца, а не побочный эффект.
    """
    if product is None:
        return {}
    out: dict = {}
    if (product.voice_id or "").strip():
        out["voice_id"] = product.voice_id.strip()
    if (product.voice_reply_mode or "").strip():
        out["reply_mode"] = product.voice_reply_mode.strip()
    return out


async def linked_assets(session: AsyncSession, product_id: int | None) -> list[MediaAsset]:
    """Материалы, привязанные к товару, в заданном порядке.

    Инструменты отправки медиа смотрят сюда ПЕРВЫМ делом: фото одного товара
    не должно улетать клиенту, который спрашивает про другой.
    """
    if not product_id:
        return []
    rows = (
        await session.execute(
            select(MediaAsset)
            .join(ProductMedia, ProductMedia.asset_id == MediaAsset.id)
            .where(ProductMedia.product_id == product_id, MediaAsset.is_active.is_(True))
            .order_by(ProductMedia.sort_order, ProductMedia.id)
        )
    ).scalars()
    return list(rows)
