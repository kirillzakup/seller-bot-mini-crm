"""Enumerations shared across models, plus an enum-column helper.

Enums are stored as their lowercase string ``value`` (portable, readable in the
DB) rather than the Python member name.
"""

from __future__ import annotations

from enum import Enum, StrEnum

from sqlalchemy import Enum as SAEnum


class Channel(StrEnum):
    """Messaging platform a contact talks to us through."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"


class OrderStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"  # link / requisites sent
    PENDING_CONFIRMATION = "pending_confirmation"  # manual receipt uploaded, awaiting operator
    PAID = "paid"  # payment verified
    FULFILLED = "fulfilled"  # access delivered
    CANCELED = "canceled"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"


class PaymentProviderType(StrEnum):
    MANUAL = "manual"  # bank transfer (Kaspi/Halyk/Freedom/Alatau) + operator confirm
    LINK = "link"  # a ready-made payment link the merchant pastes; pay + receipt
    EPAY = "epay"  # Halyk ePay gateway
    FREEDOMPAY = "freedompay"  # Freedom Pay gateway
    YOOKASSA = "yookassa"  # YooKassa (RU)
    CRYPTOBOT = "cryptobot"  # @CryptoBot


# Human-friendly labels for the admin dropdown / lists.
PAYMENT_TYPE_LABELS: dict[PaymentProviderType, str] = {
    PaymentProviderType.MANUAL: "Ручной перевод (чек)",
    PaymentProviderType.LINK: "Ссылка на оплату",
    PaymentProviderType.EPAY: "Halyk ePay",
    PaymentProviderType.FREEDOMPAY: "Freedom Pay",
    PaymentProviderType.YOOKASSA: "YooKassa",
    PaymentProviderType.CRYPTOBOT: "CryptoBot (крипта)",
}


class FulfillmentType(StrEnum):
    TEXT_LINK = "text_link"  # send text/link from the product card
    CHANNEL_INVITE = "channel_invite"  # one-time invite link to a private channel/group
    KEY_POOL = "key_pool"  # issue a license key from a pool
    FILE = "file"  # send a file
    EXTERNAL_WEBHOOK = "external_webhook"  # call merchant API to provision access


class KbKind(StrEnum):
    FAQ = "faq"
    OBJECTION = "objection"
    INFO = "info"


class TicketStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    CLOSED = "closed"


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    OPERATOR = "operator"
    SYSTEM = "system"


class LeadStatus(StrEnum):
    """Стадия заявки в работе менеджера.

    NEW — карточка висит в группе, её ещё никто не забрал.
    IN_WORK — менеджер нажал «Забрал в работу», идёт общение.
    Остальные три — исходы: продажа, отказ, не целевой (не наш клиент).
    """

    NEW = "new"
    IN_WORK = "in_work"
    WAITING = "waiting"
    SALE = "sale"
    REJECT = "reject"
    NOT_TARGET = "not_target"


def enum_column(enum_cls: type[Enum]) -> SAEnum:
    """Build a portable (VARCHAR + CHECK) enum column storing member values."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        length=32,
    )


# Незакрытая заявка: по клиенту уже работает менеджер. «В ожидании» сюда входит —
# иначе open_lead завёл бы клиенту вторую карточку, пока он ждёт своего дня.
OPEN_LEAD_STATUSES = (LeadStatus.NEW, LeadStatus.IN_WORK, LeadStatus.WAITING)
