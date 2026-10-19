"""Direct payment-link provider.

The merchant pastes a ready payment URL (Kaspi pay link, Halyk link, any hosted
payment page). Like manual transfer, it has no webhook — the customer pays via
the link and sends a receipt, which an operator confirms.
"""

from __future__ import annotations

from app.db.models.enums import PaymentProviderType, PaymentStatus
from app.payments.base import PaymentProvider, PaymentRequest, PaymentResult


class LinkPaymentProvider(PaymentProvider):
    type = PaymentProviderType.LINK

    async def create_payment(self, req: PaymentRequest) -> PaymentResult:
        url = (self.config or {}).get("url")
        if not url:
            raise RuntimeError("Ссылка на оплату не задана в способе оплаты")
        return PaymentResult(status=PaymentStatus.PENDING, checkout_url=url)
