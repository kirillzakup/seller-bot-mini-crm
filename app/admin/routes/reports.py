"""Админка «📊 Аналитика чатов».

Три действия на одной странице: разобрать переписку за период, применить
предложенные моделью правки (с откатом) и выгрузить контакты — в шаблоне
Meta для ретаргета или упрощённым списком для обзвона.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import AsyncSessionLocal, get_session
from app.db.models import ChatReport
from app.services import chat_report as svc
from app.services import contacts_export as contacts_svc
from app.services.docx_report import DocxUnavailable, build_report_docx
from app.services.settings_service import get_setting

router = APIRouter(prefix="/reports")
logger = logging.getLogger(__name__)

# Strong refs so background analysis tasks aren't garbage-collected mid-run.
_tasks: set[asyncio.Task] = set()


async def _run_analysis(report_id: int, max_dialogs: int) -> None:
    """Фоновый разбор: заполняет уже созданную запись отчёта (статус running → ready/error).

    Разбор всех диалогов — это десятки LLM-вызовов и минуты; в фоне он не упирается в
    таймаут прокси, а страница показывает «идёт анализ» и обновляется сама.
    """
    async with AsyncSessionLocal() as session:
        report = await session.get(ChatReport, report_id)
        if report is None:
            return
        try:
            await svc.run_report_into(session, report, max_dialogs=max_dialogs)
            await session.commit()
        except Exception:  # noqa: BLE001 — сбой разбора не должен ронять воркер
            logger.exception("chat report analysis failed for %s", report_id)
            await session.rollback()
            async with AsyncSessionLocal() as s2:
                r = await s2.get(ChatReport, report_id)
                if r is not None:
                    r.status = "error"
                    r.summary = "Внутренняя ошибка при анализе — попробуйте ещё раз."
                    await s2.commit()


def _redirect(note: str = "", report_id: int | None = None) -> RedirectResponse:
    url = "/admin/reports"
    params = []
    if report_id:
        params.append(f"report_id={report_id}")
    if note:
        params.append(f"note={quote(note)}")
    if params:
        url += "?" + "&".join(params)
    return RedirectResponse(url, status_code=303)


def _parse_date(raw: str, fallback: date) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return fallback


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    """Ответ-файл с именем на кириллице.

    Заголовки HTTP кодируются latin-1, поэтому русское имя нельзя класть в
    ``filename=`` — сервер упадёт с UnicodeEncodeError ещё до отдачи файла.
    Настоящее имя уходит в ``filename*`` (RFC 5987), а в ``filename=``
    остаётся ASCII-запасной вариант для старых клиентов.
    """
    ascii_name = filename.encode("ascii", "ignore").decode().strip(" _-")
    if not ascii_name or ascii_name.startswith("."):
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "dat"
        ascii_name = f"export.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.get("")
async def page(
    request: Request,
    note: str = "",
    report_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    default_from, default_to = svc.default_period()
    pf = _parse_date(date_from, default_from)
    pt = _parse_date(date_to, default_to)

    reports = await svc.list_reports(session)
    current: ChatReport | None = None
    if report_id:
        current = await session.get(ChatReport, report_id)
    elif reports:
        current = reports[0]

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "active": "reports",
            "reports": reports,
            "report": current,
            "period_from": pf.isoformat(),
            "period_to": pt.isoformat(),
            "dialogs_available": await svc.count_dialogs_in_period(session, pf, pt),
            "note": note,
        },
    )


@router.post("/generate")
async def generate(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    date_from: str = Form(""),
    date_to: str = Form(""),
    max_dialogs: int = Form(40),
):
    default_from, default_to = svc.default_period()
    pf = _parse_date(date_from, default_from)
    pt = _parse_date(date_to, default_to)
    if pf > pt:
        pf, pt = pt, pf

    # Создаём запись «идёт анализ» и коммитим, чтобы фоновая задача её сразу увидела,
    # затем запускаем разбор всех диалогов в фоне (десятки вызовов модели, минуты).
    report = await svc.create_running_report(session, period_from=pf, period_to=pt)
    await session.commit()
    cap = max(1, min(int(max_dialogs or svc._MAX_DIALOGS_HARD), svc._MAX_DIALOGS_HARD))
    task = asyncio.create_task(_run_analysis(report.id, cap))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _redirect("Анализирую все диалоги за период — это займёт минуту-другую.", report.id)


@router.post("/{report_id}/apply")
async def apply(
    report_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    ok, message = await svc.apply_report(session, report_id)
    return _redirect(message, report_id if ok else report_id)


@router.post("/{report_id}/rollback")
async def rollback(
    report_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    _, message = await svc.rollback_report(session, report_id)
    return _redirect(message, report_id)


@router.get("/{report_id}/docx")
async def download_docx(
    report_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    report = await session.get(ChatReport, report_id)
    if report is None:
        return _redirect("Отчёт не найден.")
    shop = await get_setting(session, "shop_name") or ""
    try:
        data = build_report_docx(report, shop_name=str(shop))
    except DocxUnavailable as e:
        return _redirect(str(e), report_id)
    name = f"Отчёт_по_чатам_{report.period_from:%Y-%m-%d}_{report.period_to:%Y-%m-%d}.docx"
    return _attachment(
        data,
        name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


async def _contacts_for_export(
    session: AsyncSession,
    *,
    date_from: str,
    date_to: str,
    only_with_phone: bool,
    segment: str,
    report_id: int | None,
    enrich: bool,
):
    """Общая сборка контактов для обеих выгрузок."""
    pf = _parse_date(date_from, date.today() - timedelta(days=365))
    pt = _parse_date(date_to, date.today())

    user_ids: list[int] | None = None
    if report_id and segment:
        report = await session.get(ChatReport, report_id)
        for s in (report.proposals or {}).get("segments", []) if report else []:
            if isinstance(s, dict) and str(s.get("name")) == segment:
                ids = s.get("user_ids") or []
                user_ids = [int(i) for i in ids if str(i).isdigit() or isinstance(i, int)]
                break

    contacts = await contacts_svc.collect_contacts(
        session,
        period_from=pf,
        period_to=pt,
        only_with_phone=only_with_phone,
        user_ids=user_ids,
    )
    if enrich and contacts:
        await contacts_svc.enrich_with_llm(session, contacts)
    return contacts


@router.get("/export/meta")
async def export_meta(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    date_from: str = "",
    date_to: str = "",
    segment: str = "",
    report_id: int | None = None,
    enrich: int = 0,
):
    contacts = await _contacts_for_export(
        session,
        date_from=date_from,
        date_to=date_to,
        only_with_phone=True,  # Meta без идентификатора строку не примет
        segment=segment,
        report_id=report_id,
        enrich=bool(enrich),
    )
    csv_text = contacts_svc.meta_audience_csv(contacts)
    return _attachment(
        csv_text.encode("utf-8"),
        contacts_svc.export_filename("meta_audience"),
        "text/csv; charset=utf-8",
    )


@router.get("/export/simple")
async def export_simple(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    date_from: str = "",
    date_to: str = "",
    segment: str = "",
    report_id: int | None = None,
    enrich: int = 1,
):
    contacts = await _contacts_for_export(
        session,
        date_from=date_from,
        date_to=date_to,
        only_with_phone=False,
        segment=segment,
        report_id=report_id,
        enrich=bool(enrich),
    )
    csv_text = contacts_svc.simple_csv(contacts)
    return _attachment(
        csv_text.encode("utf-8"),
        contacts_svc.export_filename("контакты"),
        "text/csv; charset=utf-8",
    )
