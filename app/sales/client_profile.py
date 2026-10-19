"""Портрет клиента: кто он, что строит, чего хочет и что мешает ему решиться.

Зачем. История переписки в промпте обрезается, а модель в каждом ходе видит её
заново и легко упускает детали: переспрашивает город, додумывает материал стен,
забывает, что клиент уже выбрал цвет или сказал «сам позвоню». Портрет — короткая
выжимка всего диалога, которая обновляется после каждого ответа и всегда стоит в
системном промпте (см. app/sales/adaptation.py).

Обновление идёт в фоне ПОСЛЕ ответа клиенту: собственный ответ бота от этого не
замедляется, а следующий ход уже видит свежий портрет.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, User
from app.db.models.enums import ConversationRole
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

# Строковые поля портрета. Пустое значение из новой выжимки НЕ затирает старое:
# клиент назвал город в начале, а последние 40 сообщений о нём уже молчат.
TEXT_FIELDS = (
    "name", "city", "language", "russian_level", "age_band", "role", "object",
    "size", "preferences", "rejected", "budget", "deadline",
    "decision_makers", "main_need", "blockers", "choice_stress", "stance", "emotion",
    "motive",
)
# Списки: возражения копятся, а вопросы без ответа — заменяются целиком (на старый
# вопрос бот мог уже ответить).
_ACCUMULATED_LISTS = ("objections",)
_REPLACED_LISTS = ("open_questions",)
_LIST_LIMIT = 6

# После таких слов клиента дожимы только раздражают — см. nudges_allowed.
STOP_STANCES = frozenset({"сам свяжется", "отказался", "купил"})

_ALLOWED = {
    "language": {"ru", "kk", "mixed"},
    "russian_level": {"свободно", "с ошибками", "слабо"},
    "age_band": {"до 35", "35-55", "55+"},
    "role": {"хозяин", "прораб/строитель", "дизайнер/архитектор", "дилер"},
    "budget": {"чувствителен к цене", "не главное"},
    "choice_stress": {"высокий", "низкий"},
    "stance": {"выбирает", "думает", "сам свяжется", "отказался", "купил"},
    "emotion": {"тревога", "спешка", "недоверие", "раздражение", "интерес"},
    "motive": {"надёжность", "красота и статус", "экономия", "без хлопот", "одобрение семьи"},
}

_HISTORY_LIMIT = 40
_REFRESH_DELAY_S = 6.0  # дать обработчику закоммитить ответ, прежде чем читать историю

_EXTRACT_SYSTEM = """\
Ты — внимательный старший менеджер по продажам в Казахстане
и хороший практический психолог. Прочитай переписку клиента (К) с менеджером (М)
и составь портрет: что клиент говорит, что за этим стоит и что он чувствует.
Верни СТРОГО JSON-объект с полями ниже. Чего в переписке нет — пустая строка или
пустой список. НИЧЕГО НЕ ДОДУМЫВАЙ: если клиент не назвал объём, —
size пустой.

name — имя, которым клиент САМ представился (не ник).
city — город объекта или клиента.
language — ru | kk | mixed: на каком языке клиенту удобно; казахский русскими
  буквами или латиницей = kk.
russian_level — свободно | с ошибками | слабо (если пишет по-русски).
age_band — до 35 | 35-55 | 55+; ТОЛЬКО при явных признаках (внуки, пенсия, учёба,
  манера речи, сленг), иначе пусто.
role — хозяин | прораб/строитель | дизайнер/архитектор | дилер.
object — для чего покупает: задача или объект клиента.
size — объём: количество, размеры, площадь.
preferences — что нравится: модель, бренд, цвет, функции.
rejected — что клиенту не подошло.
budget — чувствителен к цене | не главное.
deadline — когда нужен товар.
decision_makers — кто ещё решает: жена, семья, прораб.
main_need — одна фраза: чего клиент на самом деле хочет добиться, его словами.
blockers — одна фраза: что сейчас мешает ему решиться.
emotion — тревога | спешка | недоверие | раздражение | интерес: что клиент
  чувствует по ПОСЛЕДНИМ сообщениям (тревога = боится ошибиться с выбором).
motive — надёжность | красота и статус | экономия | без хлопот | одобрение семьи:
  глубинный мотив за его вопросами.
objections — список возражений: дорого, далеко, сравнивает с другими…
open_questions — список вопросов клиента, на которые менеджер по сути ЕЩЁ НЕ ОТВЕТИЛ.
choice_stress — высокий | низкий: высокий, если клиент теряется в вариантах,
  просит посоветовать, мечется.
stance — выбирает | думает | сам свяжется | отказался | купил: настрой по
  ПОСЛЕДНИМ сообщениям клиента. «Сам позвоню», «сам напишу», «если что — свяжусь»
  = сам свяжется; «не надо», «уже решил», «не интересно», «вопрос закрыт» =
  отказался; «купил в другом месте» = купил; «подумаю», «посоветуюсь» = думает.
"""


def load(user: User) -> dict:
    """Портрет из базы; битый или пустой — пустой словарь."""
    try:
        data = json.loads(user.profile or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_text(key: str, value) -> str:
    if isinstance(value, list):  # модель иногда отдаёт список в текстовое поле
        value = ", ".join(str(v) for v in value if str(v or "").strip())
    text = " ".join(str(value or "").split())[:200]
    allowed = _ALLOWED.get(key)
    if allowed is not None and text not in allowed:
        return ""  # модель выдумала своё значение — правило под него не найдётся
    return text


def _clean_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [" ".join(str(v).split())[:160] for v in value if str(v or "").strip()]
    return items[:_LIST_LIMIT]


def merge(old: dict, new: dict) -> dict:
    """Новая выжимка поверх старой: пустое не затирает известное."""
    merged = dict(old)
    for key in TEXT_FIELDS:
        value = _clean_text(key, new.get(key))
        if value:
            merged[key] = value
    for key in _ACCUMULATED_LISTS:
        seen = list(old.get(key) or [])
        for item in _clean_list(new.get(key)):
            if item.lower() not in {s.lower() for s in seen}:
                seen.append(item)
        merged[key] = seen[-_LIST_LIMIT:]
    for key in _REPLACED_LISTS:
        merged[key] = _clean_list(new.get(key))
    return merged


def nudges_allowed(user: User) -> bool:
    """Можно ли дожимать: не после «сам позвоню», отказа или покупки в другом месте."""
    return load(user).get("stance") not in STOP_STANCES


async def _dialog_text(session: AsyncSession, user_id: int) -> str:
    rows = (
        (
            await session.execute(
                select(Message)
                .where(
                    Message.user_id == user_id,
                    Message.role.in_([ConversationRole.USER, ConversationRole.ASSISTANT,
                                      ConversationRole.OPERATOR]),
                )
                .order_by(Message.id.desc())
                .limit(_HISTORY_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    lines = []
    for m in reversed(rows):
        text = " ".join((m.content or "").split())[:400]
        if text:
            who = "К" if m.role == ConversationRole.USER else "М"
            lines.append(f"{who}: {text}")
    return "\n".join(lines)[-9000:]


async def _sync_lead_city(session: AsyncSession, user: User, profile: dict) -> None:
    """Город из портрета — в открытую заявку, если там пусто.

    В большинстве заявок город был не указан: модель заводила заявку раньше, чем
    клиент его называл, и потом уже никто не возвращался.
    """
    city = (profile.get("city") or "").strip()
    if not city:
        return
    from app.services.leads_service import open_lead  # lazy: избегаем цикла импортов

    lead = await open_lead(session, user)
    if lead is not None and not (lead.city or "").strip():
        lead.city = city[:120]


async def refresh(session: AsyncSession, user: User) -> dict | None:
    """Пересобрать портрет по переписке. None — модель выключена или ответила мусором."""
    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return None
    dialog = await _dialog_text(session, user.id)
    if not dialog:
        return None
    try:
        msg = await chat_completion(
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": dialog},
            ],
            tools=None,
            config=llm_cfg,
            response_format={"type": "json_object"},
        )
        data = json.loads((msg.get("content") or "").strip() or "{}")
    except (LLMError, LLMNotConfigured, ValueError) as e:
        logger.warning("profile %s: не обновился: %s", user.id, e)
        return None
    if not isinstance(data, dict):
        return None

    profile = merge(load(user), data)
    user.profile = json.dumps(profile, ensure_ascii=False)
    user.profile_at = datetime.now(UTC)
    await _sync_lead_city(session, user, profile)
    await session.flush()
    return profile


# Фоновые задачи держим в множестве: asyncio хранит на них только слабые ссылки,
# и незакреплённая задача может быть собрана сборщиком мусора на середине.
_tasks: set[asyncio.Task] = set()
_running: set[int] = set()


async def _refresh_later(user_id: int) -> None:
    from app.db.base import AsyncSessionLocal  # lazy: модуль импортируется до движка

    await asyncio.sleep(_REFRESH_DELAY_S)
    try:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if user is not None and await refresh(session, user) is not None:
                await session.commit()
    except Exception:  # noqa: BLE001 — портрет вспомогательный, ответ клиенту уже ушёл
        logger.exception("profile %s: фоновое обновление упало", user_id)
    finally:
        _running.discard(user_id)


def schedule_refresh(user_id: int) -> None:
    """Обновить портрет в фоне. Повторный вызов, пока обновление идёт, ничего не делает."""
    if user_id in _running:
        return
    try:
        task = asyncio.get_running_loop().create_task(_refresh_later(user_id))
    except RuntimeError:  # нет цикла событий (скрипт, тест) — портрет подождёт
        return
    _running.add(user_id)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
