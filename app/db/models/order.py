"""Order, Payment and AccessGrant models — the commerce core."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import (
    FulfillmentType,
    OrderStatus,
    PaymentProviderType,
    PaymentStatus,
    enum_column,
)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # human-friendly

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))

    amount_kzt: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    currency: Mapped[str] = mapped_column(String(8), default="KZT")

    status: Mapped[OrderStatus] = mapped_column(
        enum_column(OrderStatus), default=OrderStatus.DRAFT, index=True
    )

    payment_method_id: Mapped[int | None] = mapped_column(ForeignKey("payment_methods.id"))
    provider_type: Mapped[PaymentProviderType | None] = mapped_column(
        enum_column(PaymentProviderType)
    )
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)  # gateway payment id

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user = relationship("User", lazy="selectin")
    product = relationship("Product", lazy="selectin")
    payment_method = relationship("PaymentMethod", lazy="selectin")


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)

    provider_type: Mapped[PaymentProviderType] = mapped_column(enum_column(PaymentProviderType))
    amount_kzt: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="KZT")
    status: Mapped[PaymentStatus] = mapped_column(
        enum_column(PaymentStatus), default=PaymentStatus.PENDING, index=True
    )

    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # Guards against double-processing of the same gateway notification.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Manual flow
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))  # tg file_id of the receipt
    confirmed_by: Mapped[str | None] = mapped_column(String(120))  # operator identity
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw: Mapped[dict] = mapped_column(JSON, default=dict)  # raw gateway payload

    order = relationship("Order", lazy="selectin")


class AccessGrant(Base, TimestampMixin):
    """A delivered entitlement. ``order_id`` is UNIQUE → at most one grant per
    order, enforcing idempotent fulfillment at the database level."""

    __tablename__ = "access_grants"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)

    type: Mapped[FulfillmentType] = mapped_column(enum_column(FulfillmentType))
    channel_id: Mapped[str | None] = mapped_column(String(64))  # tg chat id for channel/group
    invite_link: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # key/text/etc.

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user = relationship("User", lazy="selectin")
    product = relationship("Product", lazy="selectin")
    order = relationship("Order", lazy="selectin")
