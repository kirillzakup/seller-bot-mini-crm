"""Правила продажи из карточки товара.

Характер, скрипт и голос задаются у каждого товара отдельно. Пустое поле
означает «взять общую настройку бота» — на этом держится совместимость со
старыми проектами, где настройка одна на всех.
"""

from __future__ import annotations

from app.db.models import Product
from app.sales.product_rules import persona_for, script_block, voice_overrides


def _p(**kw) -> Product:
    base = {"persona_prompt": "", "sales_script": "", "voice_id": "", "voice_reply_mode": ""}
    return Product(title="Товар", sku="t", price_kzt=1, **{**base, **kw})


def test_persona_falls_back_to_global():
    assert persona_for(_p(), "общий характер") == "общий характер"
    assert persona_for(None, "общий характер") == "общий характер"


def test_persona_from_card_wins():
    assert persona_for(_p(persona_prompt="характер товара"), "общий") == "характер товара"


def test_script_falls_back_to_global():
    out = script_block(_p(), "общий скрипт")
    assert "общий скрипт" in out and "Скрипт продаж" in out


def test_script_from_card_replaces_global():
    out = script_block(_p(sales_script="скрипт товара"), "общий скрипт")
    assert "скрипт товара" in out and "общий скрипт" not in out


def test_empty_script_gives_no_heading():
    """Заголовок без текста только засоряет промпт."""
    assert script_block(_p(), "") == ""
    assert script_block(None, "") == ""


def test_voice_is_global_unless_set_in_card():
    """Правило владельца: голос общий, если не поменян в карточке."""
    assert voice_overrides(_p()) == {}
    assert voice_overrides(None) == {}
    assert voice_overrides(_p(voice_id="abc"))["voice_id"] == "abc"
    assert voice_overrides(_p(voice_reply_mode="always"))["reply_mode"] == "always"


def test_voice_overrides_ignore_whitespace_only_values():
    assert voice_overrides(_p(voice_id="   ", voice_reply_mode="  ")) == {}
