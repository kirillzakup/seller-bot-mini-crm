"""Catalog models: products and configured payment methods."""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.enums import FulfillmentType, PaymentProviderType, enum_column


class ProductCategory(Base, TimestampMixin):
    """Группа товаров. ТОЛЬКО группировка — настроек внутри нет.

    Настройки продажи (характер, скрипт, голос, материалы) живут в карточке
    ТОВАРА и больше нигде: два источника одной настройки — это всегда вопрос
    «что победит», на который нет хорошего ответа.

    Группа нужна ради двух вещей: чтобы каталог не был плоским списком и чтобы
    кнопка «применить к категории» знала, в какие карточки копировать значения.
    """

    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ProductMedia(Base):
    """Материал медиатеки, привязанный к товару.

    Отдельная таблица, а не поле в медиатеке: одно и то же фото может
    относиться сразу к нескольким товарам, и дублировать файл ради этого глупо.
    """

    __tablename__ = "product_media"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL = товар вне категорий: тогда работают общие настройки бота.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id"), index=True
    )
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")

    price_kzt: Mapped[int] = mapped_column(Integer)  # whole tenge
    currency: Mapped[str] = mapped_column(String(8), default="KZT")

    # How access is delivered after payment + per-type config
    fulfillment_type: Mapped[FulfillmentType] = mapped_column(
        enum_column(FulfillmentType), default=FulfillmentType.TEXT_LINK
    )
    # text_link  -> {"text": "...", "link": "..."}
    # channel_invite -> {"chat_id": -100..., "member_limit": 1}
    fulfillment_config: Mapped[dict] = mapped_column(JSON, default=dict)

    # For subscription products (channel access): access duration in days (None = lifetime)
    subscription_days: Mapped[int | None] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    media_file_id: Mapped[str | None] = mapped_column(String(255))  # tg photo file_id

    # ── Как бот продаёт ИМЕННО этот товар ────────────────────────────────
    # Все поля необязательные. Пусто = берётся общая настройка бота, поэтому
    # карточки, которые не трогали, ведут себя ровно как раньше.
    persona_prompt: Mapped[str] = mapped_column(Text, default="")
    sales_script: Mapped[str] = mapped_column(Text, default="")
    # Голос общий по умолчанию; заполнен здесь — звучит только этот товар.
    voice_id: Mapped[str] = mapped_column(String(64), default="")
    voice_reply_mode: Mapped[str] = mapped_column(String(16), default="")


class PaymentMethod(Base, TimestampMixin):
    """A configured way to pay. Multiple manual methods are allowed (e.g. one
    row per Kazakh bank), each with its own requisites/instructions."""

    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[PaymentProviderType] = mapped_column(enum_column(PaymentProviderType))
    title: Mapped[str] = mapped_column(String(120))  # shown to client, e.g. "Kaspi перевод"
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # API gateways: credentials (merchant_id, secret_key, terminal, test flag, ...)
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # Manual transfer: human-readable requisites + instructions shown to the client
    instructions: Mapped[str] = mapped_column(Text, default="")
