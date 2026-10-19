"""Map a product's fulfillment_type to its provider implementation."""

from __future__ import annotations

from app.db.models import Product
from app.db.models.enums import FulfillmentType
from app.fulfillment.base import FulfillmentProvider
from app.fulfillment.channel_invite import ChannelInviteFulfillment
from app.fulfillment.text_link import TextLinkFulfillment

# Phase 1+: KEY_POOL, FILE, EXTERNAL_WEBHOOK
_PROVIDERS: dict[FulfillmentType, type[FulfillmentProvider]] = {
    FulfillmentType.TEXT_LINK: TextLinkFulfillment,
    FulfillmentType.CHANNEL_INVITE: ChannelInviteFulfillment,
}


def is_supported(fulfillment_type: FulfillmentType) -> bool:
    return fulfillment_type in _PROVIDERS


def build_fulfillment(product: Product) -> FulfillmentProvider:
    cls = _PROVIDERS.get(product.fulfillment_type)
    if cls is None:
        raise NotImplementedError(f"Способ выдачи '{product.fulfillment_type}' ещё не реализован")
    return cls()
