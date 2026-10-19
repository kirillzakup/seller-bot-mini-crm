"""Payment gateway webhooks + the hosted ePay widget page.

POST /webhooks/pay/{provider} : verify the callback, find the order by the code
we embedded, and (if confirmed) run the same gated confirm→fulfill path. The
flow is idempotent, so duplicate notifications are safe.
"""

from __future__ import annotations

import html
import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.bot.runner import get_bot
from app.channels.registry import get_messenger
from app.config import get_settings
from app.db.base import AsyncSessionLocal
from app.db.models import Order, PaymentMethod, Product
from app.db.models.enums import OrderStatus, PaymentProviderType, PaymentStatus
from app.payments.base import PaymentRequest
from app.payments.epay import invoice_id_for
from app.payments.registry import build_provider, is_supported
from app.services import orders as orders_svc

router = APIRouter(tags=["payment-webhooks"])
logger = logging.getLogger(__name__)

_PROVIDER_TYPES = {
    "epay": PaymentProviderType.EPAY,
    "freedompay": PaymentProviderType.FREEDOMPAY,
    "yookassa": PaymentProviderType.YOOKASSA,
    "cryptobot": PaymentProviderType.CRYPTOBOT,
}

# Карточные шлюзы всегда сообщают сумму платежа. Отсутствие поля — признак
# испорченного или урезанного колбэка, а не разрешение пропустить сверку суммы.
_AMOUNT_REQUIRED = {
    PaymentProviderType.EPAY,
    PaymentProviderType.FREEDOMPAY,
    PaymentProviderType.YOOKASSA,
}


async def _enabled_method(session, ptype: PaymentProviderType) -> PaymentMethod | None:
    return (
        (
            await session.execute(
                select(PaymentMethod)
                .where(PaymentMethod.type == ptype, PaymentMethod.enabled.is_(True))
                .order_by(PaymentMethod.id)
            )
        )
        .scalars()
        .first()
    )


async def _find_order(session, ptype: PaymentProviderType, order_code: str | None) -> Order | None:
    """Resolve the order a webhook's ``order_code`` refers to.

    ePay can't carry our human ``Order.code`` (it must be pure digits), so its
    provider returns the numeric ``invoice_id_for(order.id)`` instead — look that
    up by primary key. Every other gateway echoes our own order code back verbatim.
    """
    if not order_code:
        return None
    if ptype == PaymentProviderType.EPAY:
        try:
            return await session.get(Order, int(order_code))
        except ValueError:
            return None
    return (
        (await session.execute(select(Order).where(Order.code == order_code))).scalars().first()
    )


@router.post("/webhooks/pay/{provider}")
async def pay_webhook(provider: str, request: Request) -> Response:
    ptype = _PROVIDER_TYPES.get(provider)
    if ptype is None:
        return Response(status_code=404)

    body = await request.body()
    try:
        form = dict(await request.form())
    except Exception:  # noqa: BLE001
        form = {}
    headers = {k.lower(): v for k, v in request.headers.items()}

    async with AsyncSessionLocal() as session:
        method = await _enabled_method(session, ptype)
        if method is None:
            logger.warning("Webhook for %s but no enabled method configured", provider)
            return Response(status_code=200)

        provider_impl = build_provider(method)
        try:
            verified = await provider_impl.verify_webhook(headers=headers, body=body, form=form)
        except Exception:  # noqa: BLE001
            logger.exception("verify_webhook failed for %s", provider)
            return Response(status_code=400)

        if not verified.signature_ok:
            logger.warning("Webhook %s: signature check failed", provider)
            return Response(status_code=403)

        if verified.status != PaymentStatus.CONFIRMED or not verified.order_code:
            return Response(content="OK", media_type="text/plain")  # ack non-final updates

        order = await _find_order(session, ptype, verified.order_code)
        if order is None or order.user is None:
            logger.warning("Webhook %s: order %s not found", provider, verified.order_code)
            return Response(content="OK", media_type="text/plain")

        # Amount guard: never grant access on an underpayment. A gateway that reports
        # the paid amount must match the order; a shortfall means tampering or the wrong
        # invoice — ack (200, no retry) but do NOT confirm.
        #
        # Card gateways ALWAYS send the amount. If it is missing, the callback is
        # malformed — previously the guard was simply skipped in that case, so an
        # underpayment with the field omitted sailed through. Treat "no amount" as
        # a failed check for card providers rather than as permission.
        if verified.amount_kzt is None and ptype in _AMOUNT_REQUIRED:
            logger.warning(
                "Webhook %s: no amount in callback for %s — refusing to confirm",
                provider, order.code,
            )
            return Response(content="OK", media_type="text/plain")

        if verified.amount_kzt is not None and verified.amount_kzt < order.amount_kzt:
            logger.warning(
                "Webhook %s: paid %s < order %s for %s — refusing to confirm",
                provider, verified.amount_kzt, order.amount_kzt, order.code,
            )
            return Response(content="OK", media_type="text/plain")

        if not order.external_id and verified.external_id:
            order.external_id = verified.external_id

        # Один и тот же колбэк шлюз может прислать несколько раз (ретраи при
        # таймауте). Ключ транзакции хранится на платеже с уникальным индексом —
        # повтор распознаём и подтверждаем молча, не переоформляя оплату заново.
        idem_key = f"{provider}:{verified.external_id or verified.order_code}"
        if await orders_svc.payment_already_processed(session, idem_key):
            logger.info("Webhook %s: повтор колбэка %s — уже обработан", provider, idem_key)
            return Response(content="OK", media_type="text/plain")

        try:
            messenger = await get_messenger(session, order.user.channel)
            tg_bot = get_bot()
            await orders_svc.confirm_payment(
                session,
                order,
                by=f"webhook:{provider}",
                messenger=messenger,
                tg_bot=tg_bot,
                idempotency_key=idem_key,
            )
            await session.commit()
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.exception("Webhook %s: confirm failed for %s", provider, order.code)
            return Response(status_code=500)

    return Response(content="OK", media_type="text/plain")


@router.get("/pay/epay/{order_code}")
async def epay_widget(order_code: str) -> HTMLResponse:
    """Hosted page that initializes the Halyk ePay widget for an order."""
    async with AsyncSessionLocal() as session:
        order = (
            (await session.execute(select(Order).where(Order.code == order_code))).scalars().first()
        )
        if order is None:
            return HTMLResponse("<h3>Заказ не найден</h3>", status_code=404)
        method = await _enabled_method(session, PaymentProviderType.EPAY)
        if method is None:
            return HTMLResponse("<h3>ePay не настроен</h3>", status_code=503)
        provider_impl = build_provider(method)
        iid = invoice_id_for(order.id)
        try:
            token = await provider_impl.get_token(invoice_id=iid, amount=order.amount_kzt)
        except Exception:  # noqa: BLE001
            logger.exception("ePay token request failed")
            return HTMLResponse(
                "<h3>Не удалось начать оплату. Попробуйте позже.</h3>", status_code=502
            )
        amount = order.amount_kzt
        currency = order.currency
        terminal = method.config.get("terminal", "")
        post_link = method.config.get("post_link", "")
        product = await session.get(Product, order.product_id)
        description = (product.title if product else order.code)[:125]

    base_url = get_settings().base_url.rstrip("/")
    back_link = f"{base_url}/pay/{order_code}"
    token_js = json.dumps(token)
    # postLink/backLink must be repeated here — the OAuth token request alone does
    # NOT make the widget call Halyk's notification back; per the payment-widget
    # docs (as opposed to the separate hosted-payment-page docs) halyk.pay() reads
    # them from its own params object. Omitting them is why the first real test
    # payment went through on Halyk's side but never notified us.
    pay_params = json.dumps(
        {
            "invoiceId": iid,
            "amount": amount,
            "currency": currency,
            "terminal": terminal,
            "description": description,
            "language": "RUS",
            "postLink": post_link,
            "failurePostLink": post_link,
            "backLink": back_link,
            "failureBackLink": back_link,
        }
    )
    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Оплата заказа {html.escape(order_code)}</title>
<!-- Official Halyk ePay SDK: must load live from the provider's domain and is
     updated by Halyk, so a pinned SRI hash is intentionally not used (it would
     break the widget on their updates). Trust anchor = the provider domain. -->
<script src="https://epay.homebank.kz/payform/payment-api.js"></script>
<style>
body {{ font-family: sans-serif; max-width: 480px; margin: 40px auto;
        padding: 0 16px; text-align: center; }}
button {{ background: #0a7d2c; color: #fff; border: 0; border-radius: 10px;
          padding: 14px 28px; font-size: 16px; cursor: pointer; }}
</style>
</head><body>
<h2>Оплата заказа {html.escape(order_code)}</h2>
<p>Сумма: <b>{amount} {html.escape(currency)}</b></p>
<button id="pay">Оплатить картой</button>
<script>
const auth = {token_js};
const params = {pay_params};
params.auth = auth;
document.getElementById('pay').onclick = function () {{
  if (window.halyk && window.halyk.pay) {{ window.halyk.pay(params); }}
  else {{ alert('Платёжный виджет недоступен'); }}
}};
</script></body></html>"""
    return HTMLResponse(page)


# ── Universal payment page (one link for every channel) ───────────────────


def _page_shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px 16px;"
        "color:#0f172a;background:#f1f5f9}.card{background:#fff;border-radius:16px;padding:20px;"
        "box-shadow:0 1px 4px rgba(0,0,0,.08)}h1{font-size:18px;margin:0 0 12px}"
        ".amt{font-size:16px}.ok{color:#16a34a;font-size:18px}"
        ".btn{display:block;text-align:center;background:#0a7d2c;color:#fff;"
        "text-decoration:none;border-radius:10px;padding:14px;margin:10px 0;font-weight:600}"
        ".m{border:1px solid #e2e8f0;border-radius:10px;padding:12px;margin:10px 0}"
        ".mt{font-weight:600;margin-bottom:6px}.mi{font-size:14px;color:#334155}"
        ".hint{font-size:13px;color:#64748b;margin-top:14px}</style></head>"
        f"<body><div class=card><h1>{html.escape(title)}</h1>{body}</div></body></html>"
    )


@router.get("/pay/{order_code}")
async def payment_page(order_code: str) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        order = (
            (await session.execute(select(Order).where(Order.code == order_code))).scalars().first()
        )
        if order is None:
            return HTMLResponse(_page_shell("Заказ не найден", "<p>Проверьте ссылку.</p>"), 404)

        product = await session.get(Product, order.product_id)
        title = product.title if product else "Заказ"
        amount = f"{order.amount_kzt:,} {order.currency}".replace(",", " ")

        if order.status in (OrderStatus.PAID, OrderStatus.FULFILLED):
            return HTMLResponse(
                _page_shell(title, "<p class='ok'>✅ Оплата получена. Спасибо!</p>")
            )

        methods = (
            (
                await session.execute(
                    select(PaymentMethod)
                    .where(PaymentMethod.enabled.is_(True))
                    .order_by(PaymentMethod.sort_order, PaymentMethod.id)
                )
            )
            .scalars()
            .all()
        )
        methods = [m for m in methods if is_supported(m.type)]

    blocks = [f"<p class='amt'>К оплате: <b>{html.escape(amount)}</b></p>"]
    for m in methods:
        if m.type == PaymentProviderType.MANUAL:
            instr = (m.instructions or "").replace("\n", "<br>")
            blocks.append(
                f"<div class='m'><div class='mt'>{html.escape(m.title)}</div>"
                f"<div class='mi'>{instr}</div></div>"
            )
        elif m.type == PaymentProviderType.LINK:
            url = (m.config or {}).get("url")
            if url:
                blocks.append(
                    f"<a class='btn' href='{html.escape(url)}' target='_blank' rel='noopener'>"
                    f"Оплатить — {html.escape(m.title)}</a>"
                )
        else:
            blocks.append(
                f"<a class='btn' href='/pay/{order_code}/go/{m.id}'>"
                f"Оплатить — {html.escape(m.title)}</a>"
            )
    if len(blocks) == 1:
        blocks.append("<p>Способы оплаты пока не настроены. Напишите в чат — поможем.</p>")
    blocks.append(
        "<p class='hint'>После оплаты переводом пришлите фото чека в чат — "
        "мы подтвердим и выдадим доступ.</p>"
    )
    return HTMLResponse(_page_shell(title, "".join(blocks)))


@router.get("/pay/{order_code}/go/{method_id}")
async def payment_go(order_code: str, method_id: int):
    async with AsyncSessionLocal() as session:
        order = (
            (await session.execute(select(Order).where(Order.code == order_code))).scalars().first()
        )
        method = await session.get(PaymentMethod, method_id)
        if order is None or method is None or not method.enabled:
            return HTMLResponse(_page_shell("Недоступно", "<p>Способ оплаты недоступен.</p>"), 404)

        if method.type == PaymentProviderType.EPAY:
            return RedirectResponse(f"/pay/epay/{order_code}", status_code=302)

        if method.type == PaymentProviderType.LINK:
            url = (method.config or {}).get("url")
            if url:
                return RedirectResponse(url, status_code=302)
            return HTMLResponse(_page_shell("Оплата", "<p>Ссылка не настроена.</p>"), 502)

        product = await session.get(Product, order.product_id)
        provider = build_provider(method)
        req = PaymentRequest(
            order_id=order.id,
            order_code=order.code,
            amount_kzt=order.amount_kzt,
            currency=order.currency,
            description=product.title if product else order.code,
        )
        try:
            result = await provider.create_payment(req)
        except Exception:  # noqa: BLE001
            logger.exception("payment_go: create_payment failed for %s", order_code)
            return HTMLResponse(
                _page_shell(
                    "Ошибка",
                    "<p>Не удалось создать платёж. Вернитесь и выберите другой способ.</p>",
                ),
                502,
            )
        order.provider_type = method.type
        order.payment_method_id = method.id
        if result.external_id:
            order.external_id = result.external_id
        await session.commit()
        checkout_url = result.checkout_url

    if checkout_url:
        return RedirectResponse(checkout_url, status_code=302)
    return HTMLResponse(_page_shell("Оплата", "<p>Свяжитесь с менеджером для оплаты.</p>"))
