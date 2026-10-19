"""Deliver access by sending the text/link stored in the product card.

The universal fallback: works for any product (course link, doc, promo code,
instructions) with zero external setup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.db.models import Order, Product, User
from app.db.models.enums import FulfillmentType
from app.fulfillment.base import FulfillmentProvider, FulfillmentResult

if TYPE_CHECKING:
    from aiogram import Bot


class TextLinkFulfillment(FulfillmentProvider):
    type = FulfillmentType.TEXT_LINK

    async def grant(
        self, *, tg_bot: Bot | None, product: Product, order: Order, user: User
    ) -> FulfillmentResult:
        cfg = product.fulfillment_config or {}
        text = (cfg.get("text") or "Спасибо за покупку!").strip()
        link = cfg.get("link")
        msg = text if not link else f"{text}\n\n{link}"
        return FulfillmentResult(type=self.type, message=msg, payload={"text": text, "link": link})
