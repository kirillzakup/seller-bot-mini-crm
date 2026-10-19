"""Payment provider interface + data contracts.

Every gateway (manual, Halyk ePay, Freedom Pay, YooKassa, ...) implements the
same shape: create a payment → give the client a checkout link OR manual
requisites → later confirm via webhook (API) or operator (manual).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.db.models.enums import PaymentProviderType, PaymentStatus


def to_amount(value) -> int | None:
    """Parse a gateway amount ("500", "500.00", 500) to a whole-unit int, else None."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


@dataclass
class PaymentRequest:
    order_id: int
    order_code: str
    amount_kzt: int
    currency: str
    description: str
    return_url: str | None = None


@dataclass
class PaymentResult:
    """Outcome of ``create_payment``. Exactly one of *checkout_url* (API gateway)
    or *manual_instructions* (manual transfer) is set."""

    status: PaymentStatus
    external_id: str | None = None
    checkout_url: str | None = None
    manual_instructions: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class VerifiedPayment:
    """Result of verifying a gateway webhook/callback.

    ``order_code`` is our human order code that we embedded into the gateway
    request at creation time, used to find the order on callback.
    """

    status: PaymentStatus
    order_code: str | None = None
    external_id: str | None = None
    amount_kzt: int | None = None
    signature_ok: bool = True
    raw: dict = field(default_factory=dict)


class PaymentProvider(ABC):
    """Base class for all payment providers."""

    type: PaymentProviderType

    def __init__(self, *, config: dict | None = None, instructions: str = "") -> None:
        self.config = config or {}
        self.instructions = instructions or ""

    @abstractmethod
    async def create_payment(self, req: PaymentRequest) -> PaymentResult:
        """Create a payment and return a checkout link or manual instructions."""

    async def verify_webhook(self, *, headers: dict, body: bytes, form: dict) -> VerifiedPayment:
        """Verify an incoming webhook/callback (API gateways only)."""
        raise NotImplementedError(f"{self.type} has no webhook")

    async def poll_status(self, external_id: str) -> PaymentStatus:
        """Query current payment status (for reconciliation). Default: unknown."""
        return PaymentStatus.PENDING
