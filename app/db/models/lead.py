"""Заявка — обращение клиента, которое ведёт живой менеджер.

Заявка отличается от заказа. Заказ появляется, когда человек уже выбрал товар и
дошёл до оплаты; заявка — раньше, в момент, когда клиент впервые спросил по делу
(цена, расчёт, наличие, доставка). Между этими двумя точками и работает продавец,
и именно этот отрезок раньше нигде не фиксировался: диалоги были, а понять, кто
их вёл и чем они кончились, было нельзя.

Карточка заявки уходит в операторскую группу с кнопкой «Забрал в работу». Дальше
менеджер отмечает исход, и заявка хранит не только результат, но и сколько времени
прошло до первой реакции — по этому полю видно, кто отвечает быстро, а кто держит
клиента сутки.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import LeadStatus, enum_column


def as_utc(value: datetime) -> datetime:
    """Привести дату к aware-UTC.

    SQLite отдаёт created_at/taken_at без таймзоны, а в обработчиках мы ставим
    datetime.now(UTC). Вычитать одно из другого Python отказывается — из-за этого
    карточка молча не перерисовывалась после «Забрал в работу», а ежедневное
    напоминание падало. Все разницы дат по заявкам считаем только через это.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Lead(Base, TimestampMixin):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    # ── Что человек хочет: заполняется моделью из переписки ──────────────
    # Свободный текст, а не ссылка на каталог: клиент часто спрашивает то,
    # чего в карточках нет («что-нибудь недорогое для дачи»), и терять такой
    # запрос из-за отсутствия точного товара нельзя.
    city: Mapped[str] = mapped_column(String(120), default="")
    product: Mapped[str] = mapped_column(String(255), default="")
    request: Mapped[str] = mapped_column(Text, default="")
    volume: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(64), default="обычный")

    # ── Расчёт, который бот сделал прямо в переписке ─────────────────────
    # Отдельными полями, а не одним текстом: по ним видно, насколько
    # посчитанное расходится с тем, что в итоге продали.
    area_m2: Mapped[float | None] = mapped_column(Float)
    qty_pcs: Mapped[int | None] = mapped_column(Integer)
    calc_total_kzt: Mapped[int | None] = mapped_column(Integer)
    calc_text: Mapped[str] = mapped_column(Text, default="")

    # Откуда пришёл: канал плюс метка рекламы, если она была
    source: Mapped[str] = mapped_column(String(120), default="")

    # Сквозная аналитика Google Ads. gclid выдаёт сам Google при клике по
    # объявлению; по нему рекламный кабинет принимает офлайн-конверсию и
    # показывает, какая кампания принесла живые деньги, а не просто переходы.
    gclid: Mapped[str] = mapped_column(String(255), default="")
    utm: Mapped[str] = mapped_column(String(500), default="")

    # ── Кто ведёт ────────────────────────────────────────────────────────
    taken_by_name: Mapped[str] = mapped_column(String(120), default="")
    taken_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[LeadStatus] = mapped_column(
        enum_column(LeadStatus), default=LeadStatus.NEW, index=True
    )

    # Что сейчас по клиенту: заметка о ходе работы менеджера.
    # Отдельно от request: там запрос клиента, здесь ход работы менеджера.
    work_note: Mapped[str] = mapped_column(Text, default="")

    # Менеджер уже нажал «⏳ В работе» на карточке — дальше на её месте кнопка
    # «🕓 В ожидании». Отдельным полем, а не статусом: заявка по-прежнему «в работе»,
    # меняется только то, что менеджеру нужно следующим шагом.
    work_started: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Когда снова принести карточку (статус WAITING). Храним в UTC: SQLite пишет дату
    # без пояса, и местное время сравнивалось бы с UTC в задаче напоминаний.
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # О чём договорились с клиентом, когда откладывали: с этим менеджер и звонит
    # в назначенный день, не вспоминая разговор недельной давности.
    wait_note: Mapped[str] = mapped_column(Text, default="")

    # ── Чем кончилось ────────────────────────────────────────────────────
    amount_kzt: Mapped[int | None] = mapped_column(Integer)  # при продаже
    reject_reason: Mapped[str] = mapped_column(Text, default="")  # при отказе
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Где висит карточка в группе — чтобы дописывать в неё исход, а не плодить
    # новые сообщения под каждым нажатием.
    card_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger)

    user = relationship("User", lazy="selectin")

    @property
    def reaction_seconds(self) -> int | None:
        """Сколько секунд заявка ждала, пока её забрали. None — ещё не забрали."""
        if not self.taken_at or not self.created_at:
            return None
        delta = as_utc(self.taken_at) - as_utc(self.created_at)
        return max(0, int(delta.total_seconds()))

    @property
    def has_result(self) -> bool:
        """Доведена ли до исхода. «В работе» результатом не считается."""
        return self.status in (LeadStatus.SALE, LeadStatus.REJECT, LeadStatus.NOT_TARGET)
