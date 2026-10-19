"""Document library: files the owner uploads for the bot to send to clients in chat —
price lists, catalogs, presentations, certificates, achievements.

Stored under ``MEDIA_DIR`` and served publicly at ``/media/{filename}`` (same place as
images), so every channel can fetch them by link. The AI picks one via the
``send_document`` tool, matched on ``title`` + ``tags`` + ``category``.
"""

from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MEDIA_DIR
from app.db.models import Document
from app.knowledge.rag import _tokens  # shared simple tokenizer (keyword overlap)
from app.services.media import ensure_media_dir

logger = logging.getLogger(__name__)

# extension → send-kind (also the allow-list). Images go as photos; everything else as a
# document (file). Covers price lists, catalogs, presentations, certificates.
_DOC_EXT_KIND = {
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "document", ".xlsx": "document", ".ppt": "document", ".pptx": "document",
    ".csv": "document", ".txt": "document", ".zip": "document",
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo",
}
_MIME_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv", "text/plain": ".txt", "application/zip": ".zip",
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
}
_MAX_BYTES = 100 * 1024 * 1024  # 100 MB — matches dialog attachments; keep nginx in sync

CATEGORIES = ["прайс", "каталог", "презентация", "сертификат", "достижение", "другое"]


async def save_document(
    session: AsyncSession,
    *,
    title: str,
    data: bytes,
    content_type: str,
    orig_name: str = "",
    category: str = "другое",
    tags: str = "",
) -> Document | None:
    """Persist an uploaded document + its DB row. Returns None on a rejected file
    (unsupported type, empty, or over the 100 MB limit)."""
    if not data or len(data) > _MAX_BYTES:
        return None
    mime = (content_type or "").lower()
    ext = _MIME_EXT.get(mime) or os.path.splitext(orig_name or "")[1].lower()
    kind = _DOC_EXT_KIND.get(ext)
    if kind is None:
        return None  # not an allowed document type
    ensure_media_dir()
    filename = f"{uuid.uuid4().hex}{ext}"
    (MEDIA_DIR / filename).write_bytes(data)
    doc = Document(
        title=(title or "").strip() or (orig_name or filename),
        filename=filename,
        mime=content_type or "application/octet-stream",
        kind=kind,
        category=category if category in CATEGORIES else "другое",
        tags=(tags or "").strip(),
    )
    session.add(doc)
    await session.flush()
    return doc


async def list_documents(session: AsyncSession) -> list[Document]:
    return list(
        (
            await session.execute(
                select(Document).order_by(Document.sort_order, Document.id.desc())
            )
        ).scalars()
    )


async def active_documents(session: AsyncSession) -> list[Document]:
    return list(
        (
            await session.execute(
                select(Document)
                .where(Document.is_active.is_(True))
                .order_by(Document.sort_order, Document.id)
            )
        ).scalars()
    )


async def delete_document(session: AsyncSession, doc_id: int) -> bool:
    doc = await session.get(Document, doc_id)
    if doc is None:
        return False
    try:
        (MEDIA_DIR / doc.filename).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove document file %s", doc.filename)
    await session.delete(doc)
    return True


async def search_documents(session: AsyncSession, query: str, limit: int = 1) -> list[Document]:
    """Best-matching active documents for a free-text query (title + tags + category)."""
    items = await active_documents(session)
    q = _tokens(query)
    if not q or not items:
        return []
    scored = [(it, len(_tokens(f"{it.title} {it.tags} {it.category}") & q)) for it in items]
    scored = [(it, s) for it, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [it for it, _ in scored[:limit]]
