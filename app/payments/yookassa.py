"""YooKassa (Россия) provider. Docs: https://yookassa.ru/developers/api

YooKassa doesn't sign webhooks, so we verify a notification by re-fetching the
payment by id over the API and trusting only ``status == succeeded``.

Note: YooKassa settles in RUB (not KZT). Price such products in RUB; we send
``amount_kzt`` as the major-unit amount in the method's configured currency.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

from app.db.models.enums import PaymentProviderType, PaymentStatus
from app.payments.base import (
    PaymentProvider,
    PaymentRequest,
    PaymentResult,
    VerifiedPayment,
    to_amount,
)

logger = logging.getLogger(__name__)

API = "https://api.yookassa.ru/v3"


class YooKassaProvider(PaymentProvider):
    type = PaymentProviderType.YOOKASSA

    def _auth_header(self) -> str:
        shop_id = self.config.get("shop_id", "")
        secret = self.config.get("secret_key", "")
        token = base64.b64encode(f"{shop_id}:{secret}".encode()).decode()
        return f"Basic {token}"

    async def create_payment(self, req: PaymentRequest) -> PaymentResult:
        currency = self.config.get("currency", "RUB")
        return_url = self.config.get("return_url") or req.return_url or "https://t.me"
        body = {
            "amount": {"value": f"{req.amount_kzt}.00", "currency": currency},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": req.description,
            "metadata": {"order_code": req.order_code},
        }
        headers = {
            "Authorization": self._auth_header(),
            "Idempotence-Key": req.order_code,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.post(f"{API}/payments", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return PaymentResult(
            status=PaymentStatus.PENDING,
            external_id=data.get("id"),
            checkout_url=(data.get("confirmation") or {}).get("confirmation_url"),
        )

    async def verify_webhook(self, *, headers: dict, body: bytes, form: dict) -> VerifiedPayment:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return VerifiedPayment(status=PaymentStatus.FAILED, signature_ok=False)

        obj = payload.get("object") or {}
        payment_id = obj.get("id")
        order_code = (obj.get("metadata") or {}).get("order_code")

        # Authenticity check: re-fetch the payment from the API.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                r = await client.get(
                    f"{API}/payments/{payment_id}", headers={"Authorization": self._auth_header()}
                )
                r.raise_for_status()
                fresh = r.json()
        except httpx.HTTPError as e:
            logger.error("YooKassa verify fetch failed: %s", e)
            return VerifiedPayment(
                status=PaymentStatus.PENDING, order_code=order_code, signature_ok=False
            )

        paid = fresh.get("status") == "succeeded" and fresh.get("paid")
        return VerifiedPayment(
            status=PaymentStatus.CONFIRMED if paid else PaymentStatus.PENDING,
            order_code=(fresh.get("metadata") or {}).get("order_code", order_code),
            external_id=payment_id,
            amount_kzt=to_amount((fresh.get("amount") or {}).get("value")),
            signature_ok=True,
            raw=fresh,
        )

    async def poll_status(self, external_id: str) -> PaymentStatus:
        """Re-fetch a payment by id (used by the reconcile job)."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                r = await client.get(
                    f"{API}/payments/{external_id}",
                    headers={"Authorization": self._auth_header()},
                )
                r.raise_for_status()
                fresh = r.json()
        except httpx.HTTPError as e:
            logger.error("YooKassa poll_status failed: %s", e)
            return PaymentStatus.PENDING
        if fresh.get("status") == "succeeded" and fresh.get("paid"):
            return PaymentStatus.CONFIRMED
        return PaymentStatus.PENDING
