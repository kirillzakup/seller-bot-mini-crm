"""Admin credential check + password change.

The panel password is stored as a **bcrypt hash** in the DB (settings key
``admin_password_hash``) once the operator sets one, and falls back to the env
``ADMIN_PASSWORD`` until then — so a fresh install still logs in with the env default,
and changing the password in the panel takes over without touching ``.env``.
"""

from __future__ import annotations

import secrets

import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.settings_service import get_setting, set_setting

PW_HASH_KEY = "admin_password_hash"
MIN_PASSWORD_LEN = 8


def _hash(password: str) -> str:
    # bcrypt caps the input at 72 bytes; truncate consistently for hash + verify.
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def _check(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


async def has_custom_password(session: AsyncSession) -> bool:
    """True once a password has been set in the panel (vs the weak env default)."""
    return bool(await get_setting(session, PW_HASH_KEY))


async def verify_login(session: AsyncSession, username: str, password: str) -> bool:
    """Validate admin credentials. Username must match the configured ``admin_username``;
    the password is checked against the DB bcrypt hash when set, else the env password."""
    settings = get_settings()
    # Сравниваем БАЙТЫ, а не строки: secrets.compare_digest на строках требует
    # только ASCII и на кириллице бросает TypeError — вход с русским паролем
    # отдавал 500 вместо честного «неверный пароль», да ещё и мимо ограничителя
    # попыток, потому что до его ветки исполнение не доходило.
    if not secrets.compare_digest(
        username.encode("utf-8"), settings.admin_username.encode("utf-8")
    ):
        return False
    stored = await get_setting(session, PW_HASH_KEY)
    if stored:
        return _check(password, str(stored))
    return secrets.compare_digest(
        password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )


async def set_password(session: AsyncSession, new_password: str) -> None:
    """Persist a new admin password (stored hashed)."""
    await set_setting(session, PW_HASH_KEY, _hash(new_password))
