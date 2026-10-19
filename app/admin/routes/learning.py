"""Admin «🧠 Обучение»: the human-in-the-loop review surface for self-learning.

Approve operator answers into the knowledge base, and run/apply LLM analysis of
successful dialogs to improve the sales script. Nothing here changes the bot's
behaviour until the owner clicks — this page IS the approval gate.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.channels.registry import get_messenger
from app.db.base import get_session
from app.db.models.enums import Channel
from app.sales import style as style_svc
from app.services import learning as svc
from app.services.settings_service import get_setting, set_setting

router = APIRouter(prefix="/learning")


def _redirect(note: str = "") -> RedirectResponse:
    url = "/admin/learning" + (f"?note={quote(note)}" if note else "")
    return RedirectResponse(url, status_code=303)


@router.get("")
async def page(
    request: Request,
    note: str = "",
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "learning.html",
        {
            "active": "learning",
            "suggestions": await svc.pending_kb_suggestions(session),
            "proposals": await svc.pending_script_proposals(session),
            "script": await get_setting(session, "sales_script") or "",
            "learning": await get_setting(session, "learning") or {},
            "style_guide": await get_setting(session, "style_guide") or "",
            "style_preview": await get_setting(session, "style_preview") or "",
            "note": note,
        },
    )


@router.post("/kb/{suggestion_id}/approve")
async def approve_kb(
    suggestion_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    title: str = Form(""),
    body: str = Form(""),
    tags: str = Form(""),
):
    item = await svc.approve_kb_suggestion(
        session, suggestion_id, title=title or None, body=body or None, tags=tags
    )
    return _redirect("Добавлено в базу знаний ✅" if item else "Не удалось добавить")


@router.post("/kb/{suggestion_id}/reject")
async def reject_kb(
    suggestion_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    await svc.reject_kb_suggestion(session, suggestion_id)
    return _redirect("Скрыто")


@router.post("/analyze")
async def analyze(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    proposal, reason = await svc.analyze_successful_dialogs(session)
    if proposal is not None:
        return _redirect(f"Готово предложение по скрипту (по {proposal.based_on} диалогам) 👇")
    return _redirect(reason or "Не удалось проанализировать")


@router.post("/style/learn")
async def learn_style(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Read past Instagram DMs and distil the owner's style into an editable preview.
    Nothing is applied yet — the owner reviews and clicks «Применить»."""
    messenger = await get_messenger(session, Channel.INSTAGRAM)
    res = await style_svc.learn_style_from_ig(session, messenger, max_threads=30)
    if res.get("ok"):
        await set_setting(session, "style_preview", res["style"])
        return _redirect(
            f"Изучил {res['owner_messages']} ваших сообщений из {res['threads']} "
            "диалогов — проверьте стиль ниже 👇"
        )
    return _redirect(res.get("reason") or "Не удалось изучить стиль")


@router.post("/style/apply")
async def apply_style(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    style_text: str = Form(""),
):
    """Apply the (possibly edited) style — the bot writes in it from now on."""
    await set_setting(session, "style_guide", style_text.strip())
    await set_setting(session, "style_preview", "")  # consume the preview
    return _redirect("Стиль применён — бот теперь пишет как вы ✅")


@router.post("/style/clear")
async def clear_style(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    await set_setting(session, "style_guide", "")
    await set_setting(session, "style_preview", "")
    return _redirect("Стиль убран — бот вернулся к обычной манере")


@router.post("/script/{proposal_id}/apply")
async def apply_script(
    proposal_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    ok = await svc.apply_script_proposal(session, proposal_id)
    return _redirect("Новый скрипт применён ✅" if ok else "Не удалось применить")


@router.post("/script/{proposal_id}/reject")
async def reject_script(
    proposal_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    await svc.reject_script_proposal(session, proposal_id)
    return _redirect("Предложение отклонено")


@router.post("/settings")
async def save_settings(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    kb_capture_enabled: str = Form(None),
    kb_min_answer_len: int = Form(15),
    analyze_max_dialogs: int = Form(12),
    auto_analyze_enabled: str = Form(None),
    auto_analyze_days: int = Form(7),
):
    current = await get_setting(session, "learning") or {}
    current.update(
        {
            "kb_capture_enabled": bool(kb_capture_enabled),
            "kb_min_answer_len": max(1, int(kb_min_answer_len)),
            "analyze_max_dialogs": max(1, min(int(analyze_max_dialogs), 50)),
            "auto_analyze_enabled": bool(auto_analyze_enabled),
            "auto_analyze_days": max(1, min(int(auto_analyze_days), 90)),
        }
    )
    await set_setting(session, "learning", current)
    return _redirect("Настройки обучения сохранены")
