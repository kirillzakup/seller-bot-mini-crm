"""Continuous improvement ("self-learning") for the AI manager — WITHOUT retraining.

Two feedback loops, both human-in-the-loop (nothing goes live without the owner's
click on the «Обучение» admin page):

1. **KB from operator answers** — when a human operator replies in «Диалоги», we
   pair the client's preceding question with the operator's answer as a
   ``KbSuggestion``. Approving it creates a ``KbItem`` the bot already reuses via
   the ``search_knowledge`` tool → the bot answers like the operator did.
2. **Sales-script improvement** — an LLM reads conversations that ended in a
   purchase and proposes an improved sales script (``ScriptProposal``). Applying
   it overwrites the ``sales_script`` setting.

No model fine-tuning: "learning" here means growing the *context* (knowledge +
script) the model is grounded on, which is cheap, instant, and fully reversible.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KbItem, KbSuggestion, Message, Order, ScriptProposal
from app.db.models.enums import ConversationRole, KbKind, OrderStatus
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)

_ROLE_LABELS = {"user": "Клиент", "assistant": "Бот", "operator": "Оператор"}

LEARNING_SYSTEM = (
    "Ты — методолог продаж. Тебе дают текущий скрипт продаж бота и несколько "
    "реальных диалогов, которые ЗАКОНЧИЛИСЬ ПОКУПКОЙ. Твоя задача — улучшить "
    "скрипт: сохрани рабочие приёмы, которые видны в успешных диалогах, усиль "
    "слабые места, сохрани пошаговую структуру и деловой, дружелюбный тон. "
    "Не выдумывай факты о товарах и ценах. Верни ТОЛЬКО JSON без markdown: "
    '{"improved_script": "<полный текст нового скрипта>", '
    '"rationale": "<кратко, 2-4 пункта, что изменил и почему>"}.'
)


# ── Loop 1: KB from operator answers ─────────────────────────────────────


async def capture_operator_answer(
    session: AsyncSession, user_id: int, answer: str
) -> KbSuggestion | None:
    """Turn an operator's reply + the client's preceding question into a pending
    ``KbSuggestion``. Returns the row, or None when skipped (disabled, too short,
    no question, or duplicate)."""
    cfg = await get_setting(session, "learning") or {}
    if not cfg.get("kb_capture_enabled", True):
        return None
    answer = (answer or "").strip()
    min_len = int(cfg.get("kb_min_answer_len", 15) or 15)
    if len(answer) < min_len:
        return None

    last_question = (
        (
            await session.execute(
                select(Message)
                .where(Message.user_id == user_id, Message.role == ConversationRole.USER)
                .order_by(Message.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if last_question is None:
        return None
    question = (last_question.content or "").strip()
    if not question:
        return None

    duplicate = (
        (
            await session.execute(
                select(KbSuggestion).where(
                    KbSuggestion.status == "pending",
                    KbSuggestion.question == question,
                    KbSuggestion.answer == answer,
                )
            )
        )
        .scalars()
        .first()
    )
    if duplicate is not None:
        return None

    suggestion = KbSuggestion(question=question, answer=answer, source="operator")
    session.add(suggestion)
    await session.flush()
    return suggestion


async def pending_kb_suggestions(session: AsyncSession, limit: int = 50) -> list[KbSuggestion]:
    return list(
        (
            await session.execute(
                select(KbSuggestion)
                .where(KbSuggestion.status == "pending")
                .order_by(KbSuggestion.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def approve_kb_suggestion(
    session: AsyncSession,
    suggestion_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: str = "",
) -> KbItem | None:
    """Promote a pending suggestion into a real KB entry the bot can retrieve."""
    suggestion = await session.get(KbSuggestion, suggestion_id)
    if suggestion is None or suggestion.status != "pending":
        return None
    item = KbItem(
        kind=KbKind.FAQ,
        title=(title or suggestion.question).strip()[:255],
        body=(body or suggestion.answer).strip(),
        tags=(tags or "").strip(),
        is_active=True,
    )
    session.add(item)
    suggestion.status = "approved"
    await session.flush()
    return item


async def reject_kb_suggestion(session: AsyncSession, suggestion_id: int) -> bool:
    suggestion = await session.get(KbSuggestion, suggestion_id)
    if suggestion is None or suggestion.status != "pending":
        return False
    suggestion.status = "rejected"
    await session.flush()
    return True


# ── Loop 2: sales-script improvement from successful dialogs ──────────────


def _parse_proposal(content: str) -> tuple[str, str]:
    """Extract (improved_script, rationale) from the model output. Tolerates code
    fences and non-JSON output (falls back to treating the whole text as script)."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        improved = str(data.get("improved_script", "")).strip()
        rationale = str(data.get("rationale", "")).strip()
        if improved:
            return improved, rationale
    except (ValueError, TypeError, AttributeError):
        pass
    # Not JSON — if the model just wrote a script, use it as-is.
    return (content or "").strip(), ""


async def _successful_transcripts(session: AsyncSession, limit: int) -> list[str]:
    paid_user_ids = list(
        (
            await session.execute(
                select(Order.user_id)
                .where(Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLED]))
                .distinct()
            )
        ).scalars()
    )
    transcripts: list[str] = []
    for uid in paid_user_ids[:limit]:
        msgs = list(
            (
                await session.execute(
                    select(Message).where(Message.user_id == uid).order_by(Message.id)
                )
            ).scalars()
        )
        if not msgs:
            continue
        lines = [
            f"{_ROLE_LABELS.get(m.role.value, m.role.value)}: {m.content}" for m in msgs[-40:]
        ]
        transcripts.append("\n".join(lines))
    return transcripts


async def analyze_successful_dialogs(
    session: AsyncSession, *, max_dialogs: int | None = None
) -> tuple[ScriptProposal | None, str | None]:
    """Ask the LLM to improve the sales script from paid conversations.

    Returns ``(proposal, None)`` on success or ``(None, reason)`` when there's
    nothing to analyze or the model call fails."""
    cfg = await get_setting(session, "learning") or {}
    limit = int(max_dialogs or cfg.get("analyze_max_dialogs", 12) or 12)
    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return None, "LLM не настроен — укажи base_url и ключ в ⚙️ Настройки."

    transcripts = await _successful_transcripts(session, limit)
    if not transcripts:
        return None, "Пока нет диалогов, закончившихся оплатой, — анализировать нечего."

    current_script = await get_setting(session, "sales_script") or ""
    user_msg = (
        "Текущий скрипт продаж:\n"
        f"{current_script}\n\n"
        "=== Диалоги, закончившиеся ПОКУПКОЙ ===\n"
        f"{chr(10).join(chr(10) + '--- диалог ---' + chr(10) + t for t in transcripts)}\n\n"
        'Верни ТОЛЬКО JSON: {"improved_script": "...", "rationale": "..."}.'
    )
    try:
        msg = await chat_completion(
            messages=[
                {"role": "system", "content": LEARNING_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            tools=None,
            config=llm_cfg,
        )
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("analyze_successful_dialogs LLM call failed: %s", e)
        return None, f"Ошибка обращения к модели: {e}"

    improved, rationale = _parse_proposal(msg.get("content") or "")
    if not improved:
        return None, "Модель не вернула улучшенный скрипт — попробуй ещё раз."

    proposal = ScriptProposal(
        proposed_text=improved, rationale=rationale, based_on=len(transcripts)
    )
    session.add(proposal)
    await session.flush()
    return proposal, None


async def pending_script_proposals(session: AsyncSession, limit: int = 20) -> list[ScriptProposal]:
    return list(
        (
            await session.execute(
                select(ScriptProposal)
                .where(ScriptProposal.status == "pending")
                .order_by(ScriptProposal.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def apply_script_proposal(session: AsyncSession, proposal_id: int) -> bool:
    """Apply a proposal → overwrite the live ``sales_script`` setting."""
    proposal = await session.get(ScriptProposal, proposal_id)
    if proposal is None or proposal.status != "pending":
        return False
    await set_setting(session, "sales_script", proposal.proposed_text)
    proposal.status = "applied"
    await session.flush()
    return True


async def reject_script_proposal(session: AsyncSession, proposal_id: int) -> bool:
    proposal = await session.get(ScriptProposal, proposal_id)
    if proposal is None or proposal.status != "pending":
        return False
    proposal.status = "rejected"
    await session.flush()
    return True
