"""Reply and inline keyboards used by the bot."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.db.models import Product


def _tenge(amount: int) -> str:
    return f"{amount:,} ₸".replace(",", " ")


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог")],
            [KeyboardButton(text="🛟 Поддержка"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def start_kb() -> InlineKeyboardMarkup:
    """Inline CTA shown under the /start greeting — kicks off the dialogue."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Хочу узнать подробнее", callback_data="start:more")],
        ]
    )


def catalog_kb(products: Sequence[Product]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.title} — {_tenge(p.price_kzt)}",
                callback_data=f"cat:product:{p.id}",
            )
        ]
        for p in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_kb(product: Product) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить", callback_data=f"cat:buy:{product.id}")],
            [InlineKeyboardButton(text="← Каталог", callback_data="cat:list")],
        ]
    )
