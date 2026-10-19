"""Learn the shop's real writing style from past Instagram DM conversations.

Not model retraining — one LLM pass reads the owner's real replies and distils a
short, reusable *style guide* that gets appended to the sales system prompt so the
bot writes like the human. Human-in-the-loop: the owner previews and applies it.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

# Enough dialog to characterise a voice without blowing the context window.
_MAX_CORPUS_CHARS = 12000

_STYLE_SYSTEM = (
    "Ты — аналитик стиля общения. Тебе дают реальные переписки менеджера "
    "компании с клиентами в Instagram Direct: реплики менеджера помечены "
    "[МЕНЕДЖЕР], клиента — [КЛИЕНТ]. Изучи ТОЛЬКО реплики [МЕНЕДЖЕР] и составь чёткую "
    "инструкцию по стилю, чтобы другой человек или бот писал точно так же.\n"
    "Опиши по пунктам:\n"
    "- Тон и обращение (на «вы»/«ты», «Вы» с большой буквы, тепло/по-деловому)\n"
    "- Длина и структура сообщений, пунктуация\n"
    "- Эмодзи: какие и как часто\n"
    "- Фирменные фразы: приветствие, как зовёт к оплате/брони, как просит контакты\n"
    "- Чего менеджер НЕ делает (что выбивалось бы из стиля)\n"
    "Верни ГОТОВУЮ инструкцию (не пересказ переписок), 6–14 строк, на русском. "
    "Без вступлений вроде «вот анализ» — сразу инструкция."
)


def _format_dialogs(dialogs: list[dict], owner_username: str) -> str:
    """Flatten threads into a labelled transcript, owner = [МЕНЕДЖЕР]."""
    owner = (owner_username or "").lower()
    lines: list[str] = []
    for d in dialogs:
        for m in d.get("messages", []):
            text = (m.get("text") or "").strip()
            if not text:
                continue
            who = "[МЕНЕДЖЕР]" if (m.get("from") or "").lower() == owner else "[КЛИЕНТ]"
            lines.append(f"{who} {text}")
        lines.append("---")
    return "\n".join(lines)[:_MAX_CORPUS_CHARS]


def count_owner_messages(dialogs: list[dict], owner_username: str) -> int:
    """How many of the manager's own messages we have to learn from."""
    owner = (owner_username or "").lower()
    return sum(
        1
        for d in dialogs
        for m in d.get("messages", [])
        if (m.get("from") or "").lower() == owner and (m.get("text") or "").strip()
    )


async def learn_style_from_ig(
    session: AsyncSession, messenger, *, max_threads: int = 30
) -> dict:
    """Fetch recent IG dialogs and distil the owner's style.

    Returns ``{ok, style, threads, owner_messages, reason}``. ``ok`` is False (with a
    human-readable ``reason``) when the LLM is off, IG isn't connected, or there's no
    material — so the caller can show the owner exactly why nothing came back."""
    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return {"ok": False, "reason": "ИИ не настроен (⚙️ Настройки → ИИ)."}
    ig = ((await get_setting(session, "meta") or {}).get("instagram")) or {}
    owner_username = ig.get("username") or ""

    dialogs = await messenger.fetch_conversations(max_threads=max_threads)
    owner_msgs = count_owner_messages(dialogs, owner_username)
    if owner_msgs < 3:
        return {
            "ok": False,
            "threads": len(dialogs),
            "owner_messages": owner_msgs,
            "reason": "Слишком мало ваших сообщений в Instagram, чтобы выучить стиль.",
        }

    corpus = _format_dialogs(dialogs, owner_username)
    try:
        msg = await chat_completion(
            messages=[
                {"role": "system", "content": _STYLE_SYSTEM},
                {"role": "user", "content": corpus},
            ],
            tools=None,
            config=llm_cfg,
        )
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("style learn LLM failed: %s", e)
        return {"ok": False, "reason": f"Ошибка ИИ: {e}"}

    style = (msg.get("content") or "").strip()
    if not style:
        return {"ok": False, "reason": "ИИ вернул пустой ответ, попробуйте ещё раз."}
    return {
        "ok": True,
        "style": style,
        "threads": len(dialogs),
        "owner_messages": owner_msgs,
    }
