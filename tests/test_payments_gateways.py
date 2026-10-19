"""Gateway webhook verification tests (no live credentials needed).

Covers the security-critical bits: signature checks and paid-status detection.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from app.db.models.enums import PaymentStatus
from app.payments.cryptobot import CryptoBotProvider
from app.payments.epay import EpayProvider
from app.payments.freedompay import FreedomPayProvider, make_signature


def _cryptobot_signature(token: str, body: bytes) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_cryptobot_valid_signature_confirms():
    async def main():
        token = "TEST_TOKEN"
        prov = CryptoBotProvider(config={"api_token": token})
        payload = {
            "update_type": "invoice_paid",
            "payload": {"invoice_id": 123, "status": "paid", "payload": "ORD123"},
        }
        body = json.dumps(payload).encode()
        sig = _cryptobot_signature(token, body)
        v = await prov.verify_webhook(headers={"crypto-pay-api-signature": sig}, body=body, form={})
        assert v.signature_ok is True
        assert v.status == PaymentStatus.CONFIRMED
        assert v.order_code == "ORD123"

    asyncio.run(main())


def test_cryptobot_bad_signature_rejected():
    async def main():
        prov = CryptoBotProvider(config={"api_token": "T"})
        body = b'{"update_type":"invoice_paid","payload":{}}'
        v = await prov.verify_webhook(
            headers={"crypto-pay-api-signature": "deadbeef"}, body=body, form={}
        )
        assert v.signature_ok is False

    asyncio.run(main())


def test_freedompay_signature_roundtrip_and_tamper():
    async def main():
        secret = "secret_key"
        prov = FreedomPayProvider(config={"secret_key": secret, "result_script_name": "result"})
        form = {"pg_order_id": "ORD9", "pg_payment_id": "P1", "pg_result": "1", "pg_salt": "abc"}
        form["pg_sig"] = make_signature("result", form, secret)

        v = await prov.verify_webhook(headers={}, body=b"", form=form)
        assert v.signature_ok is True
        assert v.status == PaymentStatus.CONFIRMED
        assert v.order_code == "ORD9"

        tampered = dict(form)
        tampered["pg_amount"] = "999999"  # not covered by the original signature
        v2 = await prov.verify_webhook(headers={}, body=b"", form=tampered)
        assert v2.signature_ok is False

    asyncio.run(main())


def test_link_provider_returns_configured_url():
    async def main():
        from app.payments.base import PaymentRequest
        from app.payments.link import LinkPaymentProvider

        prov = LinkPaymentProvider(config={"url": "https://pay.example.kz/abc"})
        req = PaymentRequest(
            order_id=1, order_code="X1", amount_kzt=1000, currency="KZT", description="t"
        )
        res = await prov.create_payment(req)
        assert res.checkout_url == "https://pay.example.kz/abc"

    asyncio.run(main())


def test_epay_postlink_paid_detection():
    async def main():
        prov = EpayProvider(config={})
        paid = await prov.verify_webhook(
            headers={}, body=b"", form={"invoiceId": "ORD7", "status": "AUTH"}
        )
        assert paid.status == PaymentStatus.CONFIRMED
        assert paid.order_code == "ORD7"

        pending = await prov.verify_webhook(
            headers={}, body=b"", form={"invoiceId": "ORD7", "status": "NEW"}
        )
        assert pending.status == PaymentStatus.PENDING

    asyncio.run(main())


def test_to_amount_parses_gateway_values():
    from app.payments.base import to_amount

    assert to_amount("500") == 500
    assert to_amount("500.00") == 500
    assert to_amount(500) == 500
    assert to_amount(499.6) == 500  # rounds
    assert to_amount(None) is None
    assert to_amount("abc") is None


def test_providers_extract_amount_for_the_guard():
    """Every gateway populates amount_kzt so the webhook handler can block underpayment."""

    async def main():
        secret = "s"
        fp = FreedomPayProvider(config={"secret_key": secret, "result_script_name": "result"})
        form = {"pg_order_id": "O", "pg_payment_id": "P", "pg_result": "1",
                "pg_amount": "1500", "pg_salt": "x"}
        form["pg_sig"] = make_signature("result", form, secret)
        assert (await fp.verify_webhook(headers={}, body=b"", form=form)).amount_kzt == 1500

        token = "T"
        cb = CryptoBotProvider(config={"api_token": token})
        payload = {"update_type": "invoice_paid",
                   "payload": {"invoice_id": 1, "status": "paid", "payload": "O",
                               "fiat_amount": "1500"}}
        body = json.dumps(payload).encode()
        v = await cb.verify_webhook(
            headers={"crypto-pay-api-signature": _cryptobot_signature(token, body)},
            body=body, form={},
        )
        assert v.amount_kzt == 1500

    asyncio.run(main())


def test_epay_invoice_id_is_numeric_and_padded():
    """ePay requires invoiceID to be 6-15 digits — Order.code is alphanumeric, so the
    numeric DB id is used instead, zero-padded to at least 6 digits."""
    from app.payments.epay import invoice_id_for

    assert invoice_id_for(7) == "000007"
    assert invoice_id_for(123456) == "123456"
    assert invoice_id_for(1234567) == "1234567"  # never truncated, only padded
    assert invoice_id_for(7).isdigit() and 6 <= len(invoice_id_for(7)) <= 15


def test_epay_get_token_sends_matching_secret_hash(monkeypatch):
    """get_token's outgoing secret_hash must be exactly what verify_webhook later
    recomputes and accepts — otherwise a real PostLink callback would be rejected."""
    import httpx

    from app.payments.epay import EpayProvider, _secret_hash

    captured = {}

    async def fake_post(self, url, data=None, **kw):
        captured.update(data or {})
        return httpx.Response(200, json={"access_token": "tok"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    async def main():
        prov = EpayProvider(config={"client_id": "SHOP.KZ", "terminal": "term-guid"})
        token = await prov.get_token(invoice_id="000042", amount=1000)
        assert token == {"access_token": "tok"}

    asyncio.run(main())
    assert captured["invoiceID"] == "000042"
    assert captured["secret_hash"] == _secret_hash("000042")
    assert len(captured["secret_hash"]) <= 32


def test_epay_secret_hash_verifies_genuine_callback_without_trust_flag():
    """A callback carrying the SAME secret_hash we generated at token-request time
    must verify as authentic even with no trust_postlink override — this is the real
    signature check the fail-closed default previously had no way to satisfy."""
    from app.payments.epay import EpayProvider, _secret_hash

    async def main():
        prov = EpayProvider(config={})  # no trust_postlink — must still pass
        genuine = await prov.verify_webhook(
            headers={}, body=b"",
            form={
                "invoiceId": "000099", "status": "AUTH", "amount": "1500",
                "secret_hash": _secret_hash("000099"),
            },
        )
        assert genuine.signature_ok is True
        assert genuine.status == PaymentStatus.CONFIRMED
        assert genuine.order_code == "000099"

        forged = await prov.verify_webhook(
            headers={}, body=b"",
            form={
                "invoiceId": "000099", "status": "AUTH", "amount": "1500",
                "secret_hash": "not-the-real-one",
            },
        )
        assert forged.signature_ok is False  # wrong secret → rejected

    asyncio.run(main())


def test_epay_fails_closed_without_trust_flag():
    """ePay PostLink has no verifiable signature → fail-closed unless the owner opts in."""

    async def main():
        prov = EpayProvider(config={})  # default
        v = await prov.verify_webhook(
            headers={}, body=b"", form={"invoiceId": "O", "status": "AUTH", "amount": "1500"}
        )
        assert v.signature_ok is False  # handler will 403 → no free access
        assert v.amount_kzt == 1500

        trusted = EpayProvider(config={"trust_postlink": True})  # secured + opted in
        v2 = await trusted.verify_webhook(
            headers={}, body=b"", form={"invoiceId": "O", "status": "AUTH", "amount": "1500"}
        )
        assert v2.signature_ok is True

    asyncio.run(main())


def test_webhook_rejects_underpayment(client, monkeypatch):
    """Money-critical end-to-end: the handler NEVER confirms when paid < order amount."""
    import app.payments.webhooks as wh
    from app.db.base import AsyncSessionLocal
    from app.db.models import Order, PaymentMethod, Product, User
    from app.db.models.enums import (
        Channel,
        FulfillmentType,
        OrderStatus,
        PaymentProviderType,
        PaymentStatus,
    )
    from app.payments.base import VerifiedPayment

    code = "UNDERPAY_TEST"

    async def setup():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.TELEGRAM, external_id="under-pay-1")
            p = Product(
                sku="under-sku", title="T", price_kzt=1000,
                fulfillment_type=FulfillmentType.TEXT_LINK,
            )
            s.add_all([u, p])
            await s.flush()
            s.add(PaymentMethod(type=PaymentProviderType.CRYPTOBOT, title="cb", enabled=True))
            s.add(Order(
                code=code, user_id=u.id, product_id=p.id, amount_kzt=1000,
                currency="KZT", status=OrderStatus.AWAITING_PAYMENT,
            ))
            await s.commit()

    asyncio.run(setup())

    calls = {"n": 0}

    async def fake_confirm(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(wh.orders_svc, "confirm_payment", fake_confirm)

    def provider_paying(amount):
        class _P:
            async def verify_webhook(self, **kw):
                return VerifiedPayment(
                    status=PaymentStatus.CONFIRMED, order_code=code, amount_kzt=amount
                )

        return lambda method: _P()

    monkeypatch.setattr(wh, "build_provider", provider_paying(100))  # underpaid
    assert client.post("/webhooks/pay/cryptobot", content=b"{}").status_code == 200
    assert calls["n"] == 0  # underpayment → confirm_payment NEVER called

    monkeypatch.setattr(wh, "build_provider", provider_paying(1000))  # exact amount
    client.post("/webhooks/pay/cryptobot", content=b"{}")
    assert calls["n"] == 1  # full payment → confirmed


def test_epay_webhook_resolves_order_by_numeric_invoice_id(client, monkeypatch):
    """ePay's own PostLink never carries our alphanumeric Order.code — the webhook
    must resolve the order via the numeric invoice id (the DB primary key) instead."""
    import app.payments.webhooks as wh
    from app.db.base import AsyncSessionLocal
    from app.db.models import Order, PaymentMethod, Product, User
    from app.db.models.enums import Channel, FulfillmentType, OrderStatus, PaymentProviderType
    from app.payments.base import VerifiedPayment
    from app.payments.epay import invoice_id_for

    async def setup() -> int:
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.TELEGRAM, external_id="epay-order-1")
            p = Product(
                sku="epay-sku", title="T", price_kzt=1000,
                fulfillment_type=FulfillmentType.TEXT_LINK,
            )
            s.add_all([u, p])
            await s.flush()
            s.add(PaymentMethod(type=PaymentProviderType.EPAY, title="ePay", enabled=True))
            order = Order(
                code="ZZZAAA", user_id=u.id, product_id=p.id, amount_kzt=1000,
                currency="KZT", status=OrderStatus.AWAITING_PAYMENT,
            )
            s.add(order)
            await s.commit()
            return order.id

    order_id = asyncio.run(setup())

    calls = {"n": 0}

    async def fake_confirm(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(wh.orders_svc, "confirm_payment", fake_confirm)

    def fake_build_provider(method):
        class _P:
            async def verify_webhook(self, **kw):
                return VerifiedPayment(
                    status=PaymentStatus.CONFIRMED,
                    order_code=invoice_id_for(order_id),  # numeric, NOT order.code
                    amount_kzt=1000,
                )

        return _P()

    monkeypatch.setattr(wh, "build_provider", fake_build_provider)
    assert client.post("/webhooks/pay/epay", content=b"{}").status_code == 200
    assert calls["n"] == 1  # resolved by numeric id → confirmed
