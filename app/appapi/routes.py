"""JSON endpoints for the native «Диалоги» app. Bearer-token auth, no cookies."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.routes.dialogs import _conversations, _messages, _send_operator_text
from app.channels.registry import get_messenger
from app.config import get_settings
from app.db.base import get_session
from app.db.models import Message, User
from app.db.models.enums import ConversationRole
from app.sales import debounce
from app.services import admin_auth
from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/appapi", tags=["appapi"])

_TOKEN_MAX_AGE = 60 * 60 * 24 * 60  # 60 days — the operator logs in rarely
_SALT = "appapi-v1"


class _LoginThrottle:
    """Ограничитель попыток входа по IP.

    Без него ``/appapi/login`` перебирается со скоростью сети: ни задержки, ни
    блокировки не было. Счётчик в памяти процесса — этого достаточно, приложением
    пользуются один-два оператора, а контейнер один.
    """

    max_attempts = 7          # столько промахов подряд допустимо
    window_seconds = 900      # окно наблюдения — 15 минут
    block_seconds = 900       # и столько же длится блокировка

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked_for(self, ip: str) -> int:
        """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
        until = self._blocked_until.get(ip, 0.0)
        left = until - time.monotonic()
        if left <= 0:
            self._blocked_until.pop(ip, None)
            return 0
        return int(left) + 1

    def register_failure(self, ip: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._fails.get(ip, []) if now - t < self.window_seconds]
        recent.append(now)
        self._fails[ip] = recent
        if len(recent) >= self.max_attempts:
            self._blocked_until[ip] = now + self.block_seconds
            self._fails.pop(ip, None)

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._blocked_until.pop(ip, None)


_login_throttle = _LoginThrottle()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=_SALT)


async def require_app(authorization: str = Header("")) -> str:
    """Verify the bearer token; returns the operator name. 401 on any problem."""
    token = authorization.removeprefix("Bearer ").removeprefix("bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="no token")
    try:
        data = _serializer().loads(token, max_age=_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="bad token") from None
    return str(data.get("u") or "operator")


def _iso(dt: datetime | None) -> str | None:
    if not isinstance(dt, datetime):
        return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).astimezone(UTC).isoformat()


def _display_name(u: User) -> str:
    return (u.full_name or "").strip() or (f"@{u.username}" if u.username else "") or u.external_id


def _msg_json(m: Message) -> dict:
    return {
        "id": m.id,
        "role": m.role.value if hasattr(m.role, "value") else str(m.role),
        "text": m.content or "",
        "media_url": m.media_url,
        "media_kind": m.media_kind,
        "status": getattr(m, "status", None),
        "at": _iso(m.created_at),
    }


# ── auth ────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(
    body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)
):
    ip = (request.client.host if request.client else "") or "?"
    blocked_for = _login_throttle.blocked_for(ip)
    if blocked_for:
        logger.warning("appapi login: перебор с %s — блокировка %s с", ip, blocked_for)
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Повторите через {blocked_for} с.",
            headers={"Retry-After": str(blocked_for)},
        )

    if not await admin_auth.verify_login(session, body.username, body.password):
        _login_throttle.register_failure(ip)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    _login_throttle.reset(ip)
    token = _serializer().dumps({"u": body.username})
    return {"token": token, "user": body.username}


@router.get("/me")
async def me(op: str = Depends(require_app)):
    return {"user": op, "base_url": get_settings().base_url.rstrip("/")}


# ── conversations ───────────────────────────────────────────────────────

@router.get("/conversations")
async def conversations(
    op: str = Depends(require_app), session: AsyncSession = Depends(get_session)
):
    convos = await _conversations(session)
    out = []
    for c in convos:
        u: User = c["user"]
        last: Message = c["last"]
        out.append({
            "user_id": u.id,
            "name": _display_name(u),
            "channel": u.channel.value if hasattr(u.channel, "value") else str(u.channel),
            "phone": u.phone or (u.external_id if str(u.channel).endswith("whatsapp") else None),
            "unread": bool(u.unread),
            "bot_paused": bool(u.bot_paused),
            "last_text": (last.content or "")[:120],
            "last_role": last.role.value if hasattr(last.role, "value") else str(last.role),
            "last_at": _iso(last.created_at),
        })
    return {"conversations": out}


@router.get("/conversations/{user_id}/messages")
async def messages(
    user_id: int,
    mark_read: bool = True,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="not found")
    if mark_read and user.unread:
        user.unread = False
        await session.commit()
    msgs = await _messages(session, user_id)
    return {
        "user": {
            "user_id": user.id, "name": _display_name(user),
            "channel": user.channel.value if hasattr(user.channel, "value") else str(user.channel),
            "phone": user.phone, "bot_paused": bool(user.bot_paused),
        },
        "messages": [_msg_json(m) for m in msgs],
    }


class SendIn(BaseModel):
    text: str


@router.post("/conversations/{user_id}/send")
async def send(
    user_id: int,
    body: SendIn,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    text = (body.text or "").strip()
    if user is None:
        raise HTTPException(status_code=404, detail="not found")
    if not text:
        raise HTTPException(status_code=400, detail="empty")
    debounce.cancel(user.id)  # operator answers → cancel any pending bot reply
    try:
        messenger = await get_messenger(session, user.channel)
    except Exception:  # noqa: BLE001
        messenger = None
        logger.warning("app send: no messenger for user %s", user_id)
    err = await _send_operator_text(session, user, messenger, text)
    await session.commit()
    msgs = await _messages(session, user_id)
    return {"ok": not err, "error": err or "", "messages": [_msg_json(m) for m in msgs]}


class BotIn(BaseModel):
    paused: bool


@router.post("/conversations/{user_id}/bot")
async def bot_toggle(
    user_id: int,
    body: BotIn,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="not found")
    user.bot_paused = bool(body.paused)
    await session.commit()
    return {"bot_paused": user.bot_paused}


@router.post("/conversations/{user_id}/read")
async def mark_read(
    user_id: int,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="not found")
    user.unread = False
    await session.commit()
    return {"ok": True}


@router.get("/ping")
async def ping(
    since: int = 0,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    """Poll for new inbound: newest message id + count of new client messages."""
    max_id = (await session.execute(select(func.coalesce(func.max(Message.id), 0)))).scalar_one()
    new = (await session.execute(
        select(func.count(Message.id)).where(
            Message.id > since, Message.role == ConversationRole.USER
        )
    )).scalar_one()
    return {"max_id": int(max_id), "new": int(new)}


# ── Expo push registration ──────────────────────────────────────────────

class PushIn(BaseModel):
    token: str


@router.post("/push/register")
async def push_register(
    body: PushIn,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    """Store an Expo push token so the operator gets native notifications."""
    tok = (body.token or "").strip()
    if not tok.startswith("ExponentPushToken") and not tok.startswith("ExpoPushToken"):
        raise HTTPException(status_code=400, detail="not an expo token")
    tokens = list(await get_setting(session, "app_push_tokens") or [])
    if tok not in tokens:
        tokens.append(tok)
        tokens = tokens[-50:]  # keep it bounded
        await set_setting(session, "app_push_tokens", tokens)
        await session.commit()
    return {"ok": True, "count": len(tokens)}


@router.post("/push/unregister")
async def push_unregister(
    body: PushIn,
    op: str = Depends(require_app),
    session: AsyncSession = Depends(get_session),
):
    tokens = [t for t in (await get_setting(session, "app_push_tokens") or []) if t != body.token]
    await set_setting(session, "app_push_tokens", tokens)
    await session.commit()
    return {"ok": True, "count": len(tokens)}
