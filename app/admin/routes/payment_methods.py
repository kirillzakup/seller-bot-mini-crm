"""Admin CRUD for payment methods (manual requisites + gateway credentials)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import PaymentMethod
from app.db.models.enums import PAYMENT_TYPE_LABELS, PaymentProviderType
from app.payments.registry import is_supported

router = APIRouter(prefix="/payment-methods")


def _parse_config(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _build_config(config_json: str, config_url: str) -> dict:
    """Gateway credentials from JSON, plus a payment-link URL when provided."""
    config = _parse_config(config_json)
    url = (config_url or "").strip()
    if url:
        config["url"] = url
    return config


@router.get("")
async def list_methods(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    methods = (
        (
            await session.execute(
                select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "payment_methods_list.html",
        {
            "active": "payments",
            "methods": methods,
            "is_supported": is_supported,
            "labels": PAYMENT_TYPE_LABELS,
        },
    )


@router.get("/new")
async def new_method(request: Request, admin: str = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "payment_method_form.html",
        {
            "active": "payments",
            "method": None,
            "types": list(PaymentProviderType),
            "labels": PAYMENT_TYPE_LABELS,
        },
    )


@router.post("/new")
async def create_method(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    type: str = Form(...),
    title: str = Form(...),
    enabled: str = Form(None),
    sort_order: int = Form(0),
    instructions: str = Form(""),
    config_json: str = Form(""),
    config_url: str = Form(""),
):
    method = PaymentMethod(
        type=PaymentProviderType(type),
        title=title,
        enabled=bool(enabled),
        sort_order=sort_order,
        instructions=instructions,
        config=_build_config(config_json, config_url),
    )
    session.add(method)
    return RedirectResponse("/admin/payment-methods", status_code=303)


@router.get("/{method_id}/edit")
async def edit_method(
    method_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    method = await session.get(PaymentMethod, method_id)
    if method is None:
        return RedirectResponse("/admin/payment-methods", status_code=303)
    return templates.TemplateResponse(
        request,
        "payment_method_form.html",
        {
            "active": "payments",
            "method": method,
            "types": list(PaymentProviderType),
            "labels": PAYMENT_TYPE_LABELS,
        },
    )


@router.post("/{method_id}/edit")
async def update_method(
    method_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    type: str = Form(...),
    title: str = Form(...),
    enabled: str = Form(None),
    sort_order: int = Form(0),
    instructions: str = Form(""),
    config_json: str = Form(""),
    config_url: str = Form(""),
):
    method = await session.get(PaymentMethod, method_id)
    if method is None:
        return RedirectResponse("/admin/payment-methods", status_code=303)
    method.type = PaymentProviderType(type)
    method.title = title
    method.enabled = bool(enabled)
    method.sort_order = sort_order
    method.instructions = instructions
    method.config = _build_config(config_json, config_url)
    return RedirectResponse("/admin/payment-methods", status_code=303)


@router.post("/{method_id}/delete")
async def delete_method(
    method_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    method = await session.get(PaymentMethod, method_id)
    if method is not None:
        await session.delete(method)
    return RedirectResponse("/admin/payment-methods", status_code=303)
