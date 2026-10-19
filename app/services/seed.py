"""Seed default settings so the admin isn't empty on first run.

Каталог, база знаний и реквизиты у каждой компании свои — их заполняет владелец в
админке. Здесь только нейтральные заготовки: способ оплаты «по счёту» и два
типовых возражения, чтобы было видно, как устроены разделы.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.db.base import AsyncSessionLocal
from app.db.models import KbItem, PaymentMethod, Setting
from app.db.models.enums import KbKind, PaymentProviderType
from app.services.settings_service import default_settings

logger = logging.getLogger(__name__)


async def seed_defaults() -> None:
    async with AsyncSessionLocal() as session:
        # 1) Runtime settings
        for key, value in default_settings().items():
            if await session.get(Setting, key) is None:
                session.add(Setting(key=key, value=value))

        # 2) Payment method: invoice via manager (online links are added in the admin)
        if (await session.execute(select(func.count(PaymentMethod.id)))).scalar_one() == 0:
            session.add(
                PaymentMethod(
                    type=PaymentProviderType.MANUAL,
                    title="Оплата по счёту",
                    instructions=(
                        "Счёт на оплату и реквизиты вышлет менеджер после подтверждения "
                        "заказа.\n"
                        "После оплаты пришлите фото чека/платёжки сюда — менеджер подтвердит."
                    ),
                    sort_order=1,
                )
            )

        # 3) Knowledge base: two generic objections as an example
        if (await session.execute(select(func.count(KbItem.id)))).scalar_one() == 0:
            session.add_all(
                [
                    KbItem(
                        kind=KbKind.OBJECTION,
                        title="дорого",
                        body=(
                            "Уточните, с чем клиент сравнивает. Покажите, за что цена: "
                            "надёжность, гарантия, сервис. Предложите вариант под бюджет."
                        ),
                        sort_order=1,
                    ),
                    KbItem(
                        kind=KbKind.OBJECTION,
                        title="подумаю / сравниваю с другими",
                        body=(
                            "Предложите расчёт и коммерческое предложение, фото и примеры. "
                            "Возьмите контакт и договоритесь о времени следующего шага."
                        ),
                        sort_order=2,
                    ),
                ]
            )

        await session.commit()
    logger.info("Seed defaults complete")
