"""CryptoBot (@CryptoBot) via Crypto Pay API.

Docs: https://help.crypt.bot/crypto-pay-api
Webhook integrity: HMAC-SHA256 of the raw body, key = SHA256(api_token),
sent in the ``crypto-pay-api-signature`` header.
"""

from __future__ import annotations

import hashlib
import hmac
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


class CryptoBotProvider(PaymentProvider):
    type = PaymentProviderType.CRYPTOBOT

    @property
    def _api(self) -> str:
        return (
            "https://testnet-pay.crypt.bot/api"
            if self.config.get("testnet")
            else "https://pay.crypt.bot/api"
        )

    @property
    def _token(self) -> str:
        return self.config.get("api_token", "")

    async def create_payment(self, req: PaymentRequest) -> PaymentResult:
        body = {
            "currency_type": "fiat",
            "fiat": self.config.get("fiat", "KZT"),
            "amount": str(req.amount_kzt),
            "description": req.description[:1024],
            "payload": req.order_code,
            "paid_btn_name": "callback",
            "paid_btn_url": self.config.get("return_url", "https://t.me"),
        }
        headers = {"Crypto-Pay-API-Token": self._token}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.post(f"{self._api}/createInvoice", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        result = data.get("result") or {}
        return PaymentResult(
            status=PaymentStatus.PENDING,
            external_id=str(result.get("invoice_id", "")),
            checkout_url=result.get("pay_url") or result.get("bot_invoice_url"),
        )

    def check_signature(self, body: bytes, signature: str | None) -> bool:
        if not self._token or not signature:
            return False
        secret = hashlib.sha256(self._token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def verify_webhook(self, *, headers: dict, body: bytes, form: dict) -> VerifiedPayment:
        signature = headers.get("crypto-pay-api-signature")
        if not self.check_signature(body, signature):
            return VerifiedPayment(status=PaymentStatus.FAILED, signature_ok=False)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return VerifiedPayment(status=PaymentStatus.FAILED, signature_ok=False)

        update_type = payload.get("update_type")
        invoice = payload.get("payload") or {}
        paid = update_type == "invoice_paid" or invoice.get("status") == "paid"
        # Prefer the fiat amount we invoiced in; fall back to the crypto amount field.
        return VerifiedPayment(
            status=PaymentStatus.CONFIRMED if paid else PaymentStatus.PENDING,
            order_code=invoice.get("payload"),
            external_id=str(invoice.get("invoice_id", "")),
            amount_kzt=to_amount(invoice.get("fiat_amount") or invoice.get("amount")),
            signature_ok=True,
            raw=invoice,
        )
