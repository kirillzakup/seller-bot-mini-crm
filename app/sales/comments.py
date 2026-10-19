"""AI handling for Instagram comments.

One LLM call per comment decides: is it spam/toxic, and (if not) a short public
reply + a DM opener — all in the shop's persona. No model retraining; grounded on
the same persona the DM bot uses.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CommentFunnel
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.sales.prompts import DEFAULT_PERSONA
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

_EMPTY = {"is_spam": False, "reply": "", "dm": ""}

_SYSTEM = (
    "Ты — SMM-менеджер компании, отвечаешь на комментарии под постами "
    "в Instagram. Чем занимается компания — смотри в блоке «Персона» ниже, "
    "ничего не додумывай за него. На ОДИН комментарий сделай:\n"
    "1) is_spam — true, если это спам, реклама, ссылка на сторонний ресурс или "
    "оскорбление/токсичность; иначе false.\n"
    "2) reply — если НЕ спам: короткий тёплый публичный ответ (1–2 фразы, дружелюбно, "
    "по делу, можно 1 эмодзи, без ссылок). Если спам — пустая строка.\n"
    "3) dm — короткое личное сообщение автору. ВАЖНО: оно уже отправляется В ДИРЕКТ, "
    "поэтому НЕ зови «написать нам в Директ» — человек и так его читает в Директе. "
    "Вместо этого продолжи разговор: ответь на его вопрос по существу и задай один "
    "уточняющий вопрос, чтобы помочь с подбором. Если спам — пустая строка.\n"
    "ВАЖНО про контекст: тебе дают ОПИСАНИЕ поста и, если есть, ВСЕ комментарии под ним. "
    "Сначала прочитай их, чтобы понять, о чём пост и обсуждение, и НЕ повторять то, что уже "
    "сказано. Отвечай строго по контексту и НЕ путай, кто есть кто (например, если на фото "
    "объект нашего клиента — это его дом, а не наш офис; если это отзыв — благодари за отзыв). "
    "Отвечаешь ТОЛЬКО на один «Комментарий», а не на весь тред. Если контекста нет — "
    "отвечай нейтрально и не выдумывай.\n"
    "Не выдумывай факты о ценах, товарах и условиях. Верни ТОЛЬКО JSON без markdown: "
    '{"is_spam": true/false, "reply": "...", "dm": "..."}.'
)


async def match_funnel(session: AsyncSession, comment_text: str) -> CommentFunnel | None:
    """Return the first enabled keyword funnel whose trigger word appears in the comment
    (case-insensitive substring), or None. Deterministic — no LLM."""
    text = (comment_text or "").lower()
    if not text.strip():
        return None
    funnels = (
        (
            await session.execute(
                select(CommentFunnel).where(CommentFunnel.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    # Match the MOST SPECIFIC keyword first: a «ХОЧУ БОТА» comment also contains «хочу», so
    # longer keywords must be checked before shorter ones — otherwise a generic «ХОЧУ» funnel
    # (especially if created earlier / lower id) would steal comments meant for «ХОЧУ БОТА».
    funnels.sort(key=lambda f: (-len((f.keyword or "").strip()), f.id))
    for f in funnels:
        kw = (f.keyword or "").strip().lower()
        if kw and kw in text:
            return f
    return None


def _parse(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return dict(_EMPTY)
    return {
        "is_spam": bool(data.get("is_spam")),
        "reply": str(data.get("reply") or "").strip(),
        "dm": str(data.get("dm") or "").strip(),
    }


async def decide(
    session: AsyncSession,
    comment_text: str,
    post_caption: str | None = None,
    thread: list[dict] | None = None,
) -> dict:
    """Return ``{is_spam, reply, dm}`` for a comment (empties when LLM is off).

    ``post_caption`` is the description of the post the comment was left under and
    ``thread`` is every other comment on that post — together they stop the bot from
    misreading who/what the post is about and from repeating what was already said."""
    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return dict(_EMPTY)
    persona = (await get_setting(session, "persona_prompt") or DEFAULT_PERSONA)[:1200]
    style = (await get_setting(session, "style_guide") or "").strip()
    caption = (post_caption or "").strip()
    context = f"Описание поста, под которым комментарий:\n«{caption[:700]}»\n\n" if caption else ""
    style_block = f"Пиши в этой манере владельца:\n{style[:900]}\n\n" if style else ""
    thread_block = ""
    if thread:
        lines = [
            f"@{(c.get('from') or 'аноним')}: {(c.get('text') or '').strip()}"
            for c in thread[-25:]
            if (c.get("text") or "").strip()
        ]
        if lines:
            thread_block = (
                "Все комментарии под этим постом (для контекста обсуждения; отвечай только "
                "на «Комментарий» ниже):\n" + "\n".join(lines)[:1800] + "\n\n"
            )
    user = (
        f"Персона компании:\n{persona}\n\n"
        f"{style_block}"
        f"{context}"
        f"{thread_block}"
        f"Комментарий: «{(comment_text or '').strip()}»\n\n"
        'Верни JSON: {"is_spam": bool, "reply": "...", "dm": "..."}.'
    )
    try:
        msg = await chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            tools=None,
            config=llm_cfg,
        )
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("comment decide LLM failed: %s", e)
        return dict(_EMPTY)
    return _parse(msg.get("content") or "")
