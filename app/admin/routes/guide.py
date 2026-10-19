"""Setup wizard (live checklist) + Help/instructions pages.

The wizard isn't a stateful step-by-step form — it reads the real config from the
DB and shows ✅/⚠️ per step, so it always reflects the truth and doubles as a
launch checklist. ``build_setup_steps`` is shared with the dashboard banner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.db.models import (
    CommentFunnel,
    Creative,
    Document,
    KbItem,
    MediaAsset,
    PaymentMethod,
    Product,
)
from app.services.settings_service import get_setting

router = APIRouter()


async def build_setup_steps(session: AsyncSession) -> list[dict]:
    """Compute the launch checklist from current settings + data."""
    bot_token = (await get_setting(session, "bot_token") or "").strip()
    bot_username = await get_setting(session, "bot_username") or ""
    llm = await get_setting(session, "llm") or {}
    voice = await get_setting(session, "voice") or {}
    meta = await get_setting(session, "meta") or {}
    op = await get_setting(session, "operator_chat_id")
    ad = await get_setting(session, "admin_chat_id")

    n_products = (await session.execute(select(func.count(Product.id)))).scalar_one()
    n_methods = (await session.execute(select(func.count(PaymentMethod.id)))).scalar_one()
    n_creatives = (await session.execute(select(func.count(Creative.id)))).scalar_one()
    n_funnels = (await session.execute(select(func.count(CommentFunnel.id)))).scalar_one()
    n_kb = (await session.execute(select(func.count(KbItem.id)))).scalar_one()
    n_media = (await session.execute(select(func.count(MediaAsset.id)))).scalar_one()
    n_docs = (await session.execute(select(func.count(Document.id)))).scalar_one()
    bot_name = (await get_setting(session, "bot_name") or "").strip()

    inbox_set = bool(await get_setting(session, "operator_inbox_chat_id"))
    wa = meta.get("whatsapp") or {}
    ig = meta.get("instagram") or {}
    llm_key = bool((llm.get("api_key") or "").strip())
    voice_on = bool(voice.get("enabled") and (voice.get("api_key") or "").strip())
    channels_on = bool(wa.get("enabled") or ig.get("enabled"))

    return [
        {
            "title": "Подключить бота Telegram",
            "required": True,
            "done": bool(bot_token),
            "note": f"Подключён: @{bot_username}" if bot_username else "Токен не задан",
            "url": "/admin/settings",
            "desc": "Создайте бота у @BotFather, скопируйте токен и вставьте в поле «Токен бота». "
            "Бот запустится сразу, без перезапуска.",
        },
        {
            "title": "Настроить ИИ-менеджера",
            "required": True,
            "done": llm_key,
            "note": f"Модель: {llm.get('model', '')}" if llm_key else "API-ключ не задан",
            "url": "/admin/settings",
            "desc": "Вставьте ключ OpenAI-совместимого API (OpenAI, DeepSeek и т.п.) "
            "в разделе «ИИ». Без него бот не сможет вести диалог.",
        },
        {
            "title": "Личность и поведение бота",
            "required": True,
            "done": bool(bot_name),
            "note": f"Имя: {bot_name}" if bot_name else "Имя/пол не заданы",
            "url": "/admin/settings",
            "desc": "⚙️ Настройки → «Личность и поведение»: задайте имя, ПОЛ (иначе род в речи "
            "скачет — то «рад», то «рада»), характер (персону), скрипт продаж и манеру — всё в "
            "одном блоке.",
        },
        {
            "title": "Добавить товары",
            "required": True,
            "done": n_products > 0,
            "note": f"Товаров: {n_products}" if n_products else "Нет товаров",
            "url": "/admin/products",
            "desc": "Хотя бы один товар с ценой и способом выдачи доступа после оплаты.",
        },
        {
            "title": "Способы оплаты",
            "required": True,
            "done": n_methods > 0,
            "note": f"Методов: {n_methods}" if n_methods else "Нет способов оплаты",
            "url": "/admin/payment-methods",
            "desc": "Укажите РЕАЛЬНЫЕ реквизиты Kaspi/перевода или «Ссылку на оплату». "
            "Проверьте, что там не осталось демо-номера.",
        },
        {
            "title": "База знаний (факты и возражения)",
            "required": False,
            "done": n_kb > 0,
            "note": f"Записей: {n_kb}" if n_kb else "Пусто (рекомендуется заполнить)",
            "url": "/admin/kb",
            "desc": "📚 База знаний — факты, ответы на частые вопросы и аргументы на возражения. "
            "Бот сам подтягивает их по ходу диалога. Чем полнее — тем реже «уточню и вернусь».",
        },
        {
            "title": "Картинки и мемы",
            "required": False,
            "done": n_media > 0,
            "note": f"Картинок: {n_media}" if n_media else "Нет (опционально)",
            "url": "/admin/media",
            "desc": "🖼 Картинки — загрузите мемы и подпишите теги-поводы (радость, подумаю, "
            "тишина…). Бот сам отправит подходящую по смыслу/эмоции в диалоге, воронке или "
            "напоминании молчащим.",
        },
        {
            "title": "Документы (прайсы, каталоги, сертификаты)",
            "required": False,
            "done": n_docs > 0,
            "note": f"Документов: {n_docs}" if n_docs else "Нет (опционально)",
            "url": "/admin/documents",
            "desc": "📄 Документы — загрузите прайсы, каталоги, презентации, сертификаты, "
            "достижения. Бот сам пришлёт их клиенту, когда тот просит цены или документ.",
        },
        {
            "title": "Чат операторов",
            "required": False,
            "done": bool(op or ad),
            "note": "Настроен" if (op or ad) else "Не задан — заявки никуда не уходят",
            "url": "/admin/settings",
            "desc": "Куда бот шлёт запросы на оператора и чеки на проверку. Узнать ID чата — "
            "команда /chatid в нужном чате (или в группе с ботом).",
        },
        {
            "title": "Голос (ElevenLabs)",
            "required": False,
            "done": voice_on,
            "note": "Включён" if voice_on else "Выключен (опционально)",
            "url": "/admin/settings",
            "desc": "Опция: бот распознаёт голосовые и отвечает голосом. "
            "Нужен ключ ElevenLabs + Voice ID.",
        },
        {
            "title": "Каналы WhatsApp / Instagram",
            "required": False,
            "done": channels_on,
            "note": "Подключены" if channels_on else "Не подключены (опционально)",
            "url": "/admin/channels",
            "desc": "Опция: подключить бота к WhatsApp и Instagram через Meta "
            "(App Secret, Verify Token, токены).",
        },
        {
            "title": "Инбокс операторов в Telegram",
            "required": False,
            "done": inbox_set,
            "note": "Подключён" if inbox_set else "Не подключён (опционально)",
            "url": "/admin/help",
            "desc": "Опция: все диалоги (Telegram/WhatsApp/Instagram) — в темах Telegram-группы, "
            "отвечаете прямо в теме, надёжные пуши. Создайте супергруппу с «Темами», добавьте "
            "бота админом и отправьте в ней команду /setinbox.",
        },
        {
            "title": "Воронки по кодовому слову",
            "required": False,
            "done": n_funnels > 0,
            "note": f"Воронок: {n_funnels}" if n_funnels else "Нет (опционально)",
            "url": "/admin/funnels",
            "desc": "Опция: клиент пишет кодовое слово в комментариях под постом — бот "
            "автоматически присылает ему в личку текст или ссылку.",
        },
        {
            "title": "Креативы (лендинги)",
            "required": False,
            "done": n_creatives > 0,
            "note": f"Креативов: {n_creatives}" if n_creatives else "Нет (опционально)",
            "url": "/admin/creatives",
            "desc": "Опция: страницы-лендинги для рекламы со ссылкой в бота "
            "и статистикой по каждому креативу.",
        },
    ]


def setup_summary(steps: list[dict]) -> dict:
    required = [s for s in steps if s["required"]]
    done_required = [s for s in required if s["done"]]
    return {
        "required_total": len(required),
        "required_done": len(done_required),
        "ready": len(done_required) == len(required),
    }


@router.get("/setup")
async def setup_wizard(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    steps = await build_setup_steps(session)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"active": "setup", "steps": steps, "summary": setup_summary(steps)},
    )


@router.get("/help")
async def help_page(request: Request, admin: str = Depends(require_login)):
    return templates.TemplateResponse(request, "help.html", {"active": "help"})
