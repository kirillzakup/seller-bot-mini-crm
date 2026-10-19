"""Админка → «Заявки»: таблица обращений и срез по менеджерам.

Страница отвечает на два вопроса владельца: что происходит с заявками прямо
сейчас и кто из менеджеров их закрывает. Поэтому кроме таблицы здесь сводка по
людям — сколько взял, сколько продал, на какую сумму и как быстро отвечает.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import Lead
from app.db.models.enums import LeadStatus
from app.services import leads_export
from app.services.leads_service import contact_link
from app.services.settings_service import get_local_tz

router = APIRouter(prefix="/leads")

_STATUS_LABEL = {
    LeadStatus.NEW: "Новая",
    LeadStatus.IN_WORK: "В работе",
    LeadStatus.WAITING: "В ожидании",
    LeadStatus.SALE: "Продажа",
    LeadStatus.REJECT: "Отказ",
    LeadStatus.NOT_TARGET: "Не целевой",
}


def _parse_day(raw: str | None, fallback: date) -> date:
    try:
        return date.fromisoformat(raw) if raw else fallback
    except ValueError:
        return fallback


def _seller_rows(leads: list[Lead]) -> list[dict]:
    """Сводка по менеджерам: взял / продал / сумма / средняя реакция.

    Считаем по заявкам, которые человек забрал, а не по всем подряд, — иначе
    новые ничьи заявки размазались бы по всем и испортили конверсию.
    """
    by_name: dict[str, dict] = {}
    for lead in leads:
        name = lead.taken_by_name or ""
        if not name:
            continue
        row = by_name.setdefault(
            name,
            {"name": name, "taken": 0, "sales": 0, "amount": 0, "rejects": 0, "_delays": []},
        )
        row["taken"] += 1
        if lead.status == LeadStatus.SALE:
            row["sales"] += 1
            row["amount"] += lead.amount_kzt or 0
        elif lead.status == LeadStatus.REJECT:
            row["rejects"] += 1
        delay = lead.reaction_seconds
        if delay is not None:
            row["_delays"].append(delay)

    rows = []
    for row in by_name.values():
        delays = row.pop("_delays")
        row["avg_reaction"] = round(sum(delays) / len(delays)) if delays else None
        row["conversion"] = round(row["sales"] * 100 / row["taken"]) if row["taken"] else 0
        rows.append(row)
    return sorted(rows, key=lambda r: (-r["amount"], -r["sales"]))


async def _collect(
    session: AsyncSession, status: str | None, date_from: str | None, date_to: str | None
):
    """Заявки за период (+ нормализованные фильтры). Общая для страницы и выгрузки."""
    today = date.today()
    pf = _parse_day(date_from, today - timedelta(days=30))
    pt = _parse_day(date_to, today)

    stmt = (
        select(Lead)
        .options(selectinload(Lead.user))
        .where(Lead.created_at >= datetime.combine(pf, time.min, tzinfo=UTC))
        .where(Lead.created_at <= datetime.combine(pt, time.max, tzinfo=UTC))
        .order_by(Lead.id.desc())
    )
    if status:
        try:
            stmt = stmt.where(Lead.status == LeadStatus(status))
        except ValueError:
            status = None

    leads = list((await session.execute(stmt)).scalars().all())
    return leads, status, pf, pt


@router.get("/export.xlsx")
async def export_leads(
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Те же заявки, что на странице, — книгой Excel."""
    leads, _status, pf, pt = await _collect(session, status, date_from, date_to)
    tz = await get_local_tz(session)
    payload = leads_export.build_workbook(
        leads, _seller_rows(leads), tz_hours=int(tz.utcoffset(None).total_seconds() // 3600)
    )
    name = quote(leads_export.filename(pf.isoformat(), pt.isoformat()))
    return Response(
        payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@router.get("/conversions.csv")
async def export_conversions(
    date_from: str | None = None,
    date_to: str | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Офлайн-конверсии для Google Ads: продажи с известным идентификатором клика."""
    leads, _status, pf, pt = await _collect(session, None, date_from, date_to)
    tz = await get_local_tz(session)
    csv_text = leads_export.google_ads_conversions_csv(
        leads, tz_hours=int(tz.utcoffset(None).total_seconds() // 3600)
    )
    name = quote(f"Конверсии_Google_Ads_{pf.isoformat()}_{pt.isoformat()}.csv")
    return Response(
        csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@router.get("")
async def list_leads(
    request: Request,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    leads, status, pf, pt = await _collect(session, status, date_from, date_to)

    sales = [lead for lead in leads if lead.status == LeadStatus.SALE]
    closed = [lead for lead in leads if lead.has_result]
    delays = [d for d in (lead.reaction_seconds for lead in leads) if d is not None]
    summary = {
        "total": len(leads),
        "new": sum(1 for lead in leads if lead.status == LeadStatus.NEW),
        "in_work": sum(
            1 for lead in leads if lead.status in (LeadStatus.IN_WORK, LeadStatus.WAITING)
        ),
        "sales": len(sales),
        "amount": sum(lead.amount_kzt or 0 for lead in sales),
        "rejects": sum(1 for lead in leads if lead.status == LeadStatus.REJECT),
        "not_target": sum(1 for lead in leads if lead.status == LeadStatus.NOT_TARGET),
        # Конверсия считается от закрытых, а не от всех: заявки, которые ещё в
        # работе, исход не предрешён, и включать их в знаменатель нечестно.
        "conversion": round(len(sales) * 100 / len(closed)) if closed else 0,
        "avg_reaction": round(sum(delays) / len(delays)) if delays else None,
        # Сколько бот насчитал по всем заявкам периода — это потенциал,
        # а не выручка: показывает, какие деньги лежат в работе у менеджеров.
        "calc_total": sum(lead.calc_total_kzt or 0 for lead in leads),
    }

    return templates.TemplateResponse(
        request,
        "leads_list.html",
        {
            "active": "leads",
            "leads": leads,
            "summary": summary,
            "sellers": _seller_rows(leads),
            "statuses": list(LeadStatus),
            "status_label": _STATUS_LABEL,
            "current_status": status,
            "date_from": pf.isoformat(),
            "date_to": pt.isoformat(),
            "contact_link": contact_link,
        },
    )
