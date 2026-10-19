"""Keyword search over knowledge-base items and products.

Deliberately simple (token overlap) — no embeddings dependency. Good enough to
ground objection/FAQ answers for a focused catalog; can be upgraded to vector
search later without changing callers.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KbItem, Product

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


def _score(haystack: str, query_tokens: set[str]) -> int:
    words = _tokens(haystack)
    return len(words & query_tokens)


async def search_kb(session: AsyncSession, query: str, limit: int = 4) -> list[KbItem]:
    items = (
        (await session.execute(select(KbItem).where(KbItem.is_active.is_(True)))).scalars().all()
    )
    q = _tokens(query)
    if not q:
        return []
    scored = [(it, _score(f"{it.title} {it.body} {it.tags}", q)) for it in items]
    scored = [(it, s) for it, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [it for it, _ in scored[:limit]]


async def active_products(session: AsyncSession) -> list[Product]:
    return list(
        (
            await session.execute(
                select(Product)
                .where(Product.is_active.is_(True))
                .order_by(Product.sort_order, Product.id)
            )
        ).scalars()
    )
