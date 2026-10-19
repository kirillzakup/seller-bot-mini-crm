"""Manual transfer provider — works for any Kazakh bank (Kaspi/Halyk/Freedom/
Alatau) with no acquiring contract. The client transfers money and uploads a
receipt; an operator confirms it in the admin panel (or via inline buttons)."""

from __future__ import annotations

from app.db.models.enums import PaymentProviderType, PaymentStatus
from app.payments.base import PaymentProvider, PaymentRequest, PaymentResult


class ManualTransferProvider(PaymentProvider):
    type = PaymentProviderType.MANUAL

    async def create_payment(self, req: PaymentRequest) -> PaymentResult:
        body = self.instructions.strip() or "Свяжитесь с менеджером для оплаты."
        instructions = (
            f"{body}\n\n💰 Сумма к оплате: <b>{req.amount_kzt:,} {req.currency}</b>\n".replace(
                ",", " "
            )
            + f"🧾 Номер заказа: <code>{req.order_code}</code>\n\n"
            "После оплаты пришлите сюда <b>фото или скан чека</b> — "
            "мы проверим и выдадим доступ."
        )
        # Manual payments are confirmed out-of-band, so they start PENDING.
        return PaymentResult(status=PaymentStatus.PENDING, manual_instructions=instructions)
