"""Fulfillment provider interface + result contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.db.models import Order, Product, User
from app.db.models.enums import FulfillmentType

if TYPE_CHECKING:
    from aiogram import Bot


@dataclass
class FulfillmentResult:
    type: FulfillmentType
    message: str  # text sent to the client (the deliverable)
    invite_link: str | None = None
    channel_id: str | None = None
    payload: dict = field(default_factory=dict)
    expires_at: datetime | None = None


class FulfillmentProvider(ABC):
    type: FulfillmentType

    @abstractmethod
    async def grant(
        self, *, tg_bot: Bot | None, product: Product, order: Order, user: User
    ) -> FulfillmentResult:
        """Provision and return the access deliverable for a paid order.

        ``tg_bot`` is the Telegram bot, needed only by providers that create a
        Telegram resource (e.g. a channel invite link) regardless of the
        customer's channel. Other providers ignore it.
        """

    async def revoke(self, *, tg_bot: Bot | None, channel_id: str | None, user: User) -> None:
        """Revoke access (refund / subscription expiry). Default: no-op."""
        return None
