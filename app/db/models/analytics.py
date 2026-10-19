"""Аналитический разбор всех диалогов за период."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChatReport(Base):
    """Один проход анализа по всем диалогам в выбранном диапазоне дат.

    Жёсткие числа (диалоги, лиды, конверсия, время ответа) считаются в Python —
    модель ничего не пересчитывает и не «прикидывает». LLM читает только
    транскрипты и пишет качественную часть: где бот теряет клиента, что менять
    в скрипте и в настройках.

    Предложения СОХРАНЯЮТСЯ, но не применяются: применение — отдельное действие,
    которое кладёт прежние значения в ``backup``, чтобы откатиться одной кнопкой.
    """

    __tablename__ = "chat_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    period_from: Mapped[date] = mapped_column(Date, index=True)
    period_to: Mapped[date] = mapped_column(Date, index=True)

    dialogs_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    messages_analyzed: Mapped[int] = mapped_column(Integer, default=0)

    # Посчитано в Python, а не моделью
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    # [{title, severity, evidence, recommendation}]
    findings: Mapped[list] = mapped_column(JSON, default=list)
    # {sales_script: str, settings: {...}, segments: [{name, criteria, user_ids}]}
    proposals: Mapped[dict] = mapped_column(JSON, default=dict)

    # ready | applied | rejected | rolled_back
    status: Mapped[str] = mapped_column(String(16), default="ready", index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Снимок прежних значений настроек — единственный путь назад после «Внести изменения»
    backup: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
