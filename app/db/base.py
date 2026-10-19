"""Async SQLAlchemy engine, session factory, and declarative Base.

MVP uses ``Base.metadata.create_all`` at startup to create missing tables.
Alembic migrations are introduced in the hardening phase; until then adding a
new model simply creates its table on next boot (existing tables are untouched).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

from sqlalchemy import DateTime, event, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.config import DATA_DIR, UPLOADS_DIR, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

_engine_kwargs: dict = {
    "echo": False,
    "future": True,
    # SQLite needs check_same_thread off for async usage
    "connect_args": {"check_same_thread": False} if settings.is_sqlite else {},
}
# Under tests each coroutine may run on its own event loop; NullPool avoids
# reusing a connection across loops (the classic aiosqlite "different loop" bug).
if os.environ.get("SELLERBOT_TESTING"):
    _engine_kwargs["poolclass"] = NullPool

engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Improve SQLite concurrency (WAL) so the bot and the admin can write together.
if settings.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # SQLite has a SINGLE writer; under a burst (IG status webhooks + admin + the bot)
        # a writer that waited only 5 s used to give up with "database is locked", and the
        # webhook that hit it dropped the client's message for good (Meta already got 200).
        # 30 s comfortably outlasts any real burst. Paired with run_write_with_retry below.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class TimestampMixin:
    """Adds created_at / updated_at columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session with commit/rollback handling."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def run_write_with_retry(
    work: Callable[[AsyncSession], Awaitable[None]],
    *,
    label: str = "",
    attempts: int = 6,
) -> bool:
    """Run ``work(session)`` in a fresh session, commit, and retry on a transient
    SQLite "database is locked".

    Webhook processing is fire-and-forget — Meta already got its 200, so a dropped
    transaction is a message lost for good with no external retry. SQLite serialises
    writers, so a burst can briefly lock the one writer; retrying a fresh transaction
    recovers it. Returns True on success, False if it ultimately failed (already logged).

    NOTE: ``work`` may re-run, so keep externally-visible side effects (sending a reply)
    as late as possible. In practice lock failures happen on the FIRST statement under
    contention — before any send — so a re-run rarely double-sends.
    """
    for attempt in range(1, attempts + 1):
        async with AsyncSessionLocal() as session:
            try:
                await work(session)
                await session.commit()
                return True
            except OperationalError as exc:
                locked = "database is locked" in str(exc).lower()
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001 — rollback on a dead conn is best-effort
                    pass
                if locked and attempt < attempts:
                    # Exponential backoff with jitter so retrying writers don't resync.
                    base = min(0.2 * 2 ** (attempt - 1), 4.0)
                    await asyncio.sleep(base * (0.6 + random.random() * 0.8))
                    continue
                logger.exception("DB write failed%s", f" ({label})" if label else "")
                return False
            except IntegrityError as exc:
                # Два вебхука от нового клиента пришли одновременно, и оба создают его
                # карточку: второй падает на UNIQUE(channel, external_id). Повтор в новой
                # сессии уже найдёт клиента — без него сообщение терялось.
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001
                    pass
                if "UNIQUE constraint failed" in str(exc) and attempt < attempts:
                    await asyncio.sleep(0.1 + random.random() * 0.3)
                    continue
                logger.exception("DB write failed%s", f" ({label})" if label else "")
                return False
            except Exception:
                logger.exception("DB write failed%s", f" ({label})" if label else "")
                return False
    return False


async def init_db() -> None:
    """Create data directories and all missing tables.

    Also applies tiny additive column migrations for SQLite (``create_all`` only
    creates missing tables, never alters existing ones), so existing local DBs
    pick up new nullable columns without losing data. Real migrations are Alembic.
    """
    # Importing the models package registers every model on Base.metadata.
    from app.db import models  # noqa: F401

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.is_sqlite:
            await conn.run_sync(_ensure_sqlite_columns)


def _ensure_sqlite_columns(connection) -> None:  # noqa: ANN001
    """Add new nullable columns to existing SQLite tables if missing."""
    from sqlalchemy import text

    # (table, column, DDL type) — additive only
    wanted = [
        ("users", "source", "VARCHAR(64)"),
        ("users", "birthday", "DATE"),
        ("users", "birthday_greet", "BOOLEAN DEFAULT 0"),
        ("users", "reminders_sent", "INTEGER DEFAULT 0"),
        ("users", "reminders_enabled", "BOOLEAN DEFAULT 1"),
        ("users", "promos_enabled", "BOOLEAN DEFAULT 1"),
        ("users", "reengaged_at", "TIMESTAMP"),
        ("users", "name_confirmed", "BOOLEAN DEFAULT 0"),
        ("users", "bot_paused", "BOOLEAN DEFAULT 0"),
        ("orders", "quantity", "INTEGER DEFAULT 1"),
        ("messages", "channel_mid", "VARCHAR(255)"),
        ("messages", "status", "VARCHAR(16)"),
        ("messages", "media_url", "VARCHAR(512)"),
        ("messages", "media_kind", "VARCHAR(16)"),
        ("messages", "reaction", "VARCHAR(16)"),
        ("messages", "pinned", "BOOLEAN DEFAULT 0"),
        ("users", "inbox_topic_id", "INTEGER"),
        ("users", "unread", "BOOLEAN DEFAULT 0"),
        ("users", "ad_ref", "VARCHAR(255)"),
        ("users", "profile", "TEXT DEFAULT ''"),
        ("users", "profile_at", "TIMESTAMP"),
        ("comment_funnels", "image_id", "INTEGER"),
        # Media library grew from "images only" into circles/videos/voice/links.
        ("media_assets", "kind", "TEXT DEFAULT 'image'"),
        ("media_assets", "url", "TEXT DEFAULT ''"),
        ("media_assets", "trigger", "TEXT DEFAULT ''"),
        # Характер и скрипт продаж переехали в товарную категорию.
        ("products", "category_id", "INTEGER"),
        # Настройки продажи переехали в карточку товара (характер, скрипт, голос).
        ("products", "persona_prompt", "TEXT DEFAULT ''"),
        ("products", "sales_script", "TEXT DEFAULT ''"),
        ("products", "voice_id", "VARCHAR(64) DEFAULT ''"),
        ("products", "voice_reply_mode", "VARCHAR(16) DEFAULT ''"),
        # Расчёт, который бот сделал в переписке, прописывается в заявку.
        ("leads", "area_m2", "REAL"),
        ("leads", "qty_pcs", "INTEGER"),
        ("leads", "calc_total_kzt", "INTEGER"),
        ("leads", "calc_text", "TEXT DEFAULT ''"),
        ("leads", "work_note", "TEXT DEFAULT ''"),
        ("leads", "work_started", "BOOLEAN DEFAULT 0"),
        ("leads", "remind_at", "TIMESTAMP"),
        ("leads", "wait_note", "TEXT DEFAULT ''"),
        # Метки рекламы: по ним сводятся клик по объявлению и продажа.
        ("leads", "gclid", "VARCHAR(255) DEFAULT ''"),
        ("leads", "utm", "VARCHAR(500) DEFAULT ''"),
    ]
    for table, column, ddl in wanted:
        cols = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
        if cols and column not in cols:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
