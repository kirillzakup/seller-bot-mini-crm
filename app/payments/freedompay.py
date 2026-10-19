"""Freedom Pay (freedompay.kz, ex-PayBox) — MD5-signed form/XML API.

Signature algorithm (PayBox): md5 of  script_name ; <param values sorted by key> ;
secret_key  joined by ';' (excluding pg_sig).
Verify exact endpoints/field names against your merchant cabinet docs when keys
are available — they're isolated here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

import defusedxml.ElementTree as DET  # XXE-safe XML parsing
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

INIT_URL = "https://api.freedompay.kz/init_payment.php"


def make_signature(script_name: str, params: dict, secret: str) -> str:
    items = sorted((k, v) for k, v in params.items() if k != "pg_sig")
    parts = [script_name, *[str(v) for _, v in items], secret]
    return hashlib.md5(";".join(parts).encode()).hexdigest()


class FreedomPayProvider(PaymentProvider):
    type = PaymentProviderType.FREEDOMPAY

    async def create_payment(self, req: PaymentRequest) -> PaymentResult:
        secret = self.config.get("secret_key", "")
        params = {
            "pg_merchant_id": self.config.get("merchant_id", ""),
            "pg_order_id": req.order_code,
            "pg_amount": str(req.amount_kzt),
            "pg_currency": self.config.get("currency", "KZT"),
            "pg_description": req.description,
            "pg_salt": secrets.token_hex(8),
            "pg_result_url": self.config.get("result_url", ""),
            "pg_success_url": self.config.get("success_url", "https://t.me"),
            "pg_request_method": "POST",
            "pg_testing_mode": "1" if self.config.get("test") else "0",
        }
        params["pg_sig"] = make_signature("init_payment.php", params, secret)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.post(INIT_URL, data=params)
            resp.raise_for_status()
            text = resp.text

        try:
            root = DET.fromstring(text)
        except Exception as e:  # malformed or unsafe (XXE/DTD) XML
            raise RuntimeError(f"FreedomPay bad response: {text[:200]}") from e
        return PaymentResult(
            status=PaymentStatus.PENDING,
            external_id=root.findtext("pg_payment_id"),
            checkout_url=root.findtext("pg_redirect_url"),
        )

    def verify_callback(self, form: dict) -> bool:
        secret = self.config.get("secret_key", "")
        given = form.get("pg_sig", "")
        script_name = self.config.get("result_script_name", "result")
        expected = make_signature(script_name, form, secret)
        return bool(given) and hmac.compare_digest(given, expected)

    async def verify_webhook(self, *, headers: dict, body: bytes, form: dict) -> VerifiedPayment:
        ok = self.verify_callback(form)
        if not ok:
            return VerifiedPayment(status=PaymentStatus.FAILED, signature_ok=False)
        paid = form.get("pg_result") == "1"
        return VerifiedPayment(
            status=PaymentStatus.CONFIRMED if paid else PaymentStatus.PENDING,
            order_code=form.get("pg_order_id"),
            external_id=form.get("pg_payment_id"),
            amount_kzt=to_amount(form.get("pg_amount")),
            signature_ok=True,
            raw=dict(form),
        )
