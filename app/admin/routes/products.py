"""Admin CRUD for products."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import MediaAsset, Product, ProductCategory, ProductMedia
from app.db.models.enums import FulfillmentType
from app.fulfillment.registry import is_supported

router = APIRouter(prefix="/products")

# Папки каталога. Категория — это просто папка: создал, потом складываешь в неё
# товары. Настройки продажи в ней НЕ живут — они в карточке товара.
_SLUG_BAD = re.compile(r"[^a-z0-9-]+")
_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
        "ю": "yu", "я": "ya", " ": "-", "_": "-",
    }
)


def _slugify(name: str) -> str:
    slug = _SLUG_BAD.sub("-", name.strip().lower().translate(_TRANSLIT)).strip("-")
    return slug[:64] or "cat"


@router.post("/folder/save")
async def save_folder(
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    name: str = Form(""),
):
    """Создать папку каталога прямо со страницы «Товары»."""
    title = name.strip()
    if title:
        session.add(ProductCategory(name=title, slug=_slugify(title), is_active=True))
        await session.commit()
    return RedirectResponse("/admin/products", status_code=303)


@router.post("/folder/{cat_id}/delete")
async def delete_folder(
    cat_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    """Удалить папку. Товары остаются — просто выходят из неё."""
    cat = await session.get(ProductCategory, cat_id)
    if cat is not None:
        for prod in (
            (await session.execute(select(Product).where(Product.category_id == cat_id)))
            .scalars()
            .all()
        ):
            prod.category_id = None
        await session.delete(cat)
        await session.commit()
    return RedirectResponse("/admin/products", status_code=303)

_SUPPORTED_TYPES = [t for t in FulfillmentType if is_supported(t)]


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _slugify(text: str) -> str:
    """ASCII slug from a title; empty for non-latin titles (we fall back then)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:40]


async def _unique_sku(
    session: AsyncSession, desired: str, title: str, *, exclude_id: int | None = None
) -> str:
    """A SKU that doesn't collide. Blank input is auto-generated; collisions get a
    numeric suffix — so saving a product never fails on the unique constraint.
    """
    base = (desired or "").strip()[:60] or _slugify(title) or "tovar"
    stmt = select(Product.sku)
    if exclude_id is not None:
        stmt = stmt.where(Product.id != exclude_id)
    taken = set((await session.execute(stmt)).scalars().all())
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _build_config(
    ft: FulfillmentType, text: str, link: str, chat_id: str, member_limit: str
) -> dict:
    if ft == FulfillmentType.CHANNEL_INVITE:
        return {
            "chat_id": _int(chat_id, chat_id or None),
            "member_limit": _int(member_limit, 1) or 1,
            "text": text,
        }
    return {"text": text, "link": link or None}


async def _assets(session: AsyncSession) -> list[MediaAsset]:
    """Вся активная медиатека — из неё выбираются материалы товара."""
    return list(
        (
            await session.execute(
                select(MediaAsset)
                .where(MediaAsset.is_active.is_(True))
                .order_by(MediaAsset.kind, MediaAsset.sort_order, MediaAsset.id)
            )
        )
        .scalars()
        .all()
    )


async def _linked_ids(session: AsyncSession, product_id: int) -> set[int]:
    return set(
        (
            await session.execute(
                select(ProductMedia.asset_id).where(ProductMedia.product_id == product_id)
            )
        )
        .scalars()
        .all()
    )


async def _set_media(session: AsyncSession, product_id: int, raw_ids: list[str]) -> None:
    """Переписать привязку материалов товара под то, что отмечено в форме.

    Порядок сохраняем как в форме: он же станет порядком показа клиенту.
    """
    wanted = [int(x) for x in raw_ids if str(x).strip().isdigit()]
    for row in (
        (await session.execute(select(ProductMedia).where(ProductMedia.product_id == product_id)))
        .scalars()
        .all()
    ):
        await session.delete(row)
    await session.flush()
    for order, asset_id in enumerate(wanted):
        session.add(ProductMedia(product_id=product_id, asset_id=asset_id, sort_order=order))


async def _categories(session: AsyncSession) -> list[ProductCategory]:
    """Группы для выпадающего списка в карточке товара."""
    return list(
        (
            await session.execute(
                select(ProductCategory)
                .where(ProductCategory.is_active.is_(True))
                .order_by(ProductCategory.sort_order, ProductCategory.id)
            )
        )
        .scalars()
        .all()
    )


# Что именно копировать в другие карточки. Без выбора первая же попытка
# применить голос ко всем товарам стёрла бы индивидуальные скрипты.
_APPLY_MAP = {
    "persona": ("persona_prompt",),
    "script": ("sales_script",),
    "voice": ("voice_id", "voice_reply_mode"),
}
# Материалы копируются отдельно — это строки связи, а не поля товара.
_APPLY_MEDIA = "media"


async def _apply_to_others(
    session: AsyncSession, source: Product, scope: str, fields: set[str]
) -> int:
    """Скопировать выбранные настройки источника в другие карточки.

    Это КОПИРОВАНИЕ, а не ссылка: после применения каждая карточка живёт своей
    жизнью и правится отдельно. Возвращает число затронутых товаров.
    """
    if scope not in ("category", "all") or not fields:
        return 0
    attrs = [a for key in fields for a in _APPLY_MAP.get(key, ())]
    if not attrs:
        return 0

    query = select(Product).where(Product.id != source.id)
    if scope == "category":
        if source.category_id is None:
            return 0
        query = query.where(Product.category_id == source.category_id)

    targets = list((await session.execute(query)).scalars().all())
    for target in targets:
        for attr in attrs:
            setattr(target, attr, getattr(source, attr))
    return len(targets)


@router.get("")
async def list_products(
    request: Request,
    deleted: int | None = None,
    hidden: int | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    products = (
        (await session.execute(select(Product).order_by(Product.sort_order, Product.id)))
        .scalars()
        .all()
    )
    folders = await _categories(session)
    return templates.TemplateResponse(
        request,
        "products_list.html",
        {
            "active": "products",
            "products": products,
            "folders": folders,
            "deleted": deleted,
            "hidden": hidden,
        },
    )


@router.get("/new")
async def new_product(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "product_form.html",
        {
            "active": "products",
            "product": None,
            "types": _SUPPORTED_TYPES,
            "categories": await _categories(session),
            "assets": await _assets(session),
            "linked": set(),
        },
    )


@router.post("/new")
async def create_product(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    sku: str = Form(""),
    title: str = Form(...),
    description: str = Form(""),
    price_kzt: int = Form(...),
    fulfillment_type: str = Form(...),
    subscription_days: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form(None),
    ff_text: str = Form(""),
    ff_link: str = Form(""),
    ff_chat_id: str = Form(""),
    ff_member_limit: str = Form("1"),
):
    ft = FulfillmentType(fulfillment_type)
    product = Product(
        sku=await _unique_sku(session, sku, title),
        title=title,
        description=description,
        price_kzt=price_kzt,
        fulfillment_type=ft,
        fulfillment_config=_build_config(ft, ff_text, ff_link, ff_chat_id, ff_member_limit),
        subscription_days=_int(subscription_days),
        sort_order=sort_order,
        is_active=bool(is_active),
    )
    session.add(product)
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/{product_id}/edit")
async def edit_product(
    product_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    product = await session.get(Product, product_id)
    if product is None:
        return RedirectResponse("/admin/products", status_code=303)
    return templates.TemplateResponse(
        request,
        "product_form.html",
        {
            "active": "products",
            "product": product,
            "types": _SUPPORTED_TYPES,
            "categories": await _categories(session),
            "assets": await _assets(session),
            "linked": await _linked_ids(session, product_id),
        },
    )


@router.post("/{product_id}/edit")
async def update_product(
    product_id: int,
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    sku: str = Form(""),
    title: str = Form(...),
    description: str = Form(""),
    price_kzt: int = Form(...),
    fulfillment_type: str = Form(...),
    subscription_days: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form(None),
    ff_text: str = Form(""),
    ff_link: str = Form(""),
    ff_chat_id: str = Form(""),
    ff_member_limit: str = Form("1"),
    category_id: str = Form(""),
    persona_prompt: str = Form(""),
    sales_script: str = Form(""),
    voice_id: str = Form(""),
    voice_reply_mode: str = Form(""),
    apply_scope: str = Form("one"),
    apply_fields: list[str] = Form([]),
    media_ids: list[str] = Form([]),
):
    product = await session.get(Product, product_id)
    if product is None:
        return RedirectResponse("/admin/products", status_code=303)
    ft = FulfillmentType(fulfillment_type)
    product.sku = await _unique_sku(session, sku, title, exclude_id=product_id)
    product.title = title
    product.description = description
    product.price_kzt = price_kzt
    product.fulfillment_type = ft
    product.fulfillment_config = _build_config(ft, ff_text, ff_link, ff_chat_id, ff_member_limit)
    product.subscription_days = _int(subscription_days)
    product.sort_order = sort_order
    product.is_active = bool(is_active)
    product.category_id = _int(category_id)
    product.persona_prompt = persona_prompt.strip()
    product.sales_script = sales_script.strip()
    product.voice_id = voice_id.strip()
    product.voice_reply_mode = voice_reply_mode.strip()

    await _set_media(session, product.id, media_ids)
    copied = await _apply_to_others(session, product, apply_scope, set(apply_fields))
    note = f"?note=Применено+к+{copied}+товарам" if copied else ""
    return RedirectResponse(f"/admin/products{note}", status_code=303)


@router.post("/{product_id}/delete")
async def delete_product(
    product_id: int,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    product = await session.get(Product, product_id)
    if product is None:
        return RedirectResponse("/admin/products", status_code=303)
    try:
        await session.delete(product)
        await session.flush()  # force the FK check now so we can react to it
    except IntegrityError:
        # The product is referenced by orders — hard delete would break history.
        # Hide it from the catalog instead (soft delete) and explain why.
        await session.rollback()
        product = await session.get(Product, product_id)
        if product is not None:
            product.is_active = False
        return RedirectResponse("/admin/products?hidden=1", status_code=303)
    return RedirectResponse("/admin/products?deleted=1", status_code=303)
