"""Shared admin dependencies: templates instance + auth guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, get_settings

TEMPLATES_DIR = BASE_DIR / "app" / "admin" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Almaty is UTC+5 year-round (Kazakhstan has no DST) — safe fallback if tzdata is absent.
_FALLBACK_TZ = timezone(timedelta(hours=5))


def _tenge(value) -> str:
    try:
        return f"{int(value):,} ₸".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _localtime(value, fmt: str = "%d.%m %H:%M") -> str:
    """Render a stored datetime (naive = UTC, from SQLite ``func.now()``) in the
    configured timezone. Without this, times show as raw UTC in the panel."""
    if not isinstance(value, datetime):
        return "" if value is None else str(value)
    dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    try:
        dt = dt.astimezone(ZoneInfo(get_settings().timezone))
    except Exception:
        dt = dt.astimezone(_FALLBACK_TZ)
    return dt.strftime(fmt)


templates.env.filters["tenge"] = _tenge
templates.env.filters["localtime"] = _localtime


def require_login(request: Request) -> str:
    """Dependency: ensure an admin session exists, else redirect to login (303)."""
    user = request.session.get("admin")
    if not user:
        raise HTTPException(
            status_code=303, detail="login required", headers={"Location": "/admin/login"}
        )
    return user
