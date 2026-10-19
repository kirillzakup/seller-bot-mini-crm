"""Vision helper — ask the LLM to classify an inbound image and read its amount.

Stops the bot from accepting ANY photo (a selfie, a random picture) as a payment receipt,
and reads the paid amount so the operator is warned when it doesn't match the order.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)


@dataclass
class ReceiptAnalysis:
    """is_receipt: True/False, or None when we couldn't decide (LLM off/error/ambiguous).
    amount: paid amount in whole tenge read off the receipt, or None if not found."""

    is_receipt: bool | None
    amount: int | None


_NONE = ReceiptAnalysis(is_receipt=None, amount=None)

_RECEIPT_PROMPT = (
    "Ты проверяешь изображение. Это ПЛАТЁЖНЫЙ чек или квитанция об оплате? Чеком считается: "
    "квитанция банковского перевода, скриншот успешного платежа (Kaspi, Halyk, Каспи Голд), "
    "банковская квитанция/чек с суммой и статусом «успешно». НЕ чек: селфи или фото человека, "
    "фото товара/помещения, мем, произвольная картинка, скриншот переписки, документ без "
    "платежа. Если это чек — определи ОПЛАЧЕННУЮ сумму (итог перевода). "
    'Ответь СТРОГО в JSON без пояснений и без ```: '
    '{"is_receipt": true/false, "amount": <целое число тенге без пробелов и символов, '
    "или null если суммы не видно или это не чек>}."
)


def _parse(answer: str) -> ReceiptAnalysis:
    raw = (answer or "").strip().strip("`").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            is_r = data.get("is_receipt")
            if not isinstance(is_r, bool):
                is_r = None
            amt = data.get("amount")
            try:
                amt = int(amt) if amt is not None and str(amt).strip() != "" else None
            except (ValueError, TypeError):
                amt = None
            return ReceiptAnalysis(is_receipt=is_r, amount=amt)
    # Fallback: model ignored the JSON instruction and answered да/нет.
    low = raw.lower()
    if low.startswith(("да", "yes")):
        return ReceiptAnalysis(is_receipt=True, amount=None)
    if low.startswith(("нет", "no")):
        return ReceiptAnalysis(is_receipt=False, amount=None)
    logger.info("receipt vision unparseable answer: %r", raw[:80])
    return _NONE


async def analyze_receipt(
    session: AsyncSession, image_bytes: bytes, mime: str = "image/jpeg"
) -> ReceiptAnalysis:
    """Classify the image as a payment receipt and read its amount. On LLM off/error →
    is_receipt None so callers degrade SAFELY (still route to the operator)."""
    if not image_bytes:
        return _NONE
    config = await get_setting(session, "llm") or {}
    import base64

    data_uri = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(image_bytes).decode()}"
    messages = [
        {"role": "system", "content": _RECEIPT_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Это чек об оплате? Верни JSON с суммой."},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        },
    ]
    try:
        assistant = await chat_completion(messages=messages, tools=None, config=config)
    except (LLMNotConfigured, LLMError) as e:
        logger.warning("receipt vision check failed (degrade to operator review): %s", e)
        return _NONE
    return _parse(assistant.get("content") or "")
