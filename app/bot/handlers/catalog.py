"""Catalog browsing: list products, show a card, start checkout."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.keyboards import catalog_kb, product_kb
from app.config import get_settings
from app.db.base import AsyncSessionLocal
from app.db.models import Product
from app.services import orders as orders_svc
from app.services.users import get_or_create_telegram_user

router = Router()
logger = logging.getLogger(__name__)


def _tenge(amount: int) -> str:
    return f"{amount:,} ₸".replace(",", " ")


def _card_text(p: Product) -> str:
    desc = (p.description or "").strip()
    return f"<b>{p.title}</b>\n\n{desc}\n\n💰 Цена: <b>{_tenge(p.price_kzt)}</b>"


async def _active_products(session) -> list[Product]:
    return list(
        (
            await session.execute(
                select(Product)
                .where(Product.is_active.is_(True))
                .order_by(Product.sort_order, Product.id)
            )
        ).scalars()
    )


async def _show_catalog(message: Message) -> None:
    async with AsyncSessionLocal() as session:
        products = await _active_products(session)
    if not products:
        await message.answer("Каталог пока пуст. Загляните позже 🙏")
        return
    await message.answer("🛍 <b>Каталог</b>\nВыберите товар:", reply_markup=catalog_kb(products))


@router.message(Command("catalog"))
@router.message(F.text == "🛍 Каталог")
async def catalog_cmd(message: Message) -> None:
    await _show_catalog(message)


@router.callback_query(F.data == "cat:list")
async def catalog_back(callback: CallbackQuery) -> None:
    if callback.message:
        await _show_catalog(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cat:product:"))
async def product_card(callback: CallbackQuery) -> None:
    product_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as session:
        product = await session.get(Product, product_id)
    if not product or not product.is_active:
        await callback.answer("Товар недоступен", show_alert=True)
        return
    if callback.message:
        if product.media_file_id:
            await callback.message.answer_photo(
                product.media_file_id, caption=_card_text(product), reply_markup=product_kb(product)
            )
        else:
            await callback.message.answer(_card_text(product), reply_markup=product_kb(product))
    await callback.answer()


@router.callback_query(F.data.startswith("cat:buy:"))
async def buy(callback: CallbackQuery) -> None:
    product_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product or not product.is_active:
            await callback.answer("Товар недоступен", show_alert=True)
            return
        user = await get_or_create_telegram_user(session, callback.from_user)
        order = await orders_svc.create_order(session, user, product)
        await session.commit()
        code, title, price = order.code, product.title, product.price_kzt

    link = f"{get_settings().base_url.rstrip('/')}/pay/{code}"
    if callback.message:
        await callback.message.answer(
            f"Оформляем: <b>{title}</b> — {_tenge(price)}\n\n"
            f"💳 Ссылка на оплату:\n{link}\n\n"
            "После оплаты переводом пришлите фото чека сюда — подтвердим и выдадим доступ."
        )
    await callback.answer()


# ── Menu placeholders (full support flow arrives in Phase 2) ──────────────


@router.message(F.text == "ℹ️ Помощь")
async def help_btn(message: Message) -> None:
    await message.answer(
        "Я помогу выбрать товар и оформить заказ.\n"
        "• 🛍 Каталог — посмотреть товары\n"
        "• 🛟 Поддержка — связаться с менеджером\n"
        "Просто напишите ваш вопрос — я отвечу."
    )


@router.message(F.text == "🛟 Поддержка")
async def support_btn(message: Message) -> None:
    await message.answer("Опишите ваш вопрос одним сообщением — менеджер скоро ответит. 🛟")
