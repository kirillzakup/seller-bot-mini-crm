"""Clean text for chat + correct TTS reading (тг→тенге, с 0→с нуля, no markdown/quotes)."""

import re

from app.sales import textfmt
from app.sales.textfmt import normalize_for_tts, strip_markdown


def test_tts_spells_dimensions():
    out = normalize_for_tts("Кирпич 250x60x65 — отличный выбор")
    assert " на " in out and "250" not in out  # x separators become "на", digits spelled
    if textfmt._n2w:
        assert "двести пятьдесят на шестьдесят на шестьдесят пять" in out


def test_tts_cyrillic_and_symbol_separators():
    for src in ("250х60", "250×60", "250*60"):
        assert " на " in normalize_for_tts(src)


def test_tts_units():
    assert "процентов" in normalize_for_tts("скидка 10%")
    assert "квадратн" in normalize_for_tts("цена за м2")
    assert "миллиметр" in normalize_for_tts("толщина 12 мм")


def test_chat_text_keeps_digits():
    # strip_markdown feeds the CLIENT chat — it must NOT spell numbers out.
    assert strip_markdown("коробка 250x60x65") == "коробка 250x60x65"


def test_tts_leaves_long_ids_alone():
    assert "77000000042" in normalize_for_tts("код 77000000042")


def test_strip_markdown_removes_asterisks_backticks():
    assert strip_markdown("**Интенсив** «Нейросети»") == "Интенсив «Нейросети»"
    assert strip_markdown("цена *50 000*") == "цена 50 000"
    assert "`" not in strip_markdown("вот `код`")
    # keeps normal text + emoji
    assert strip_markdown("Отлично 🙂") == "Отлично 🙂"


def test_tts_expands_tenge():
    assert "тенге" in normalize_for_tts("Стоимость 50 000 тг.")
    assert "тг" not in normalize_for_tts("50 000 тг")
    assert normalize_for_tts("Цена — 65 000 ₸").endswith("тенге")
    assert "₸" not in normalize_for_tts("65 000 ₸")
    # a bare "т" after a number also reads as тенге
    assert normalize_for_tts("всего 4 990 т") .endswith("тенге")


def test_tts_from_zero():
    assert "с нуля" in normalize_for_tts("Интенсив «Нейросети с 0»")
    assert "с 0" not in normalize_for_tts("курс с 0")


def test_tts_strips_markdown_and_quotes():
    out = normalize_for_tts("**Курс** «Компьютерная грамотность» за 65 000 тг")
    assert "*" not in out
    assert "«" not in out and "»" not in out
    assert "тенге" in out
    assert "Курс" in out and "Компьютерная грамотность" in out


def test_tts_does_not_mangle_words_starting_with_t():
    # "тенге", "часа", "тысяч" must survive; only the abbreviation is expanded
    out = normalize_for_tts("8 занятий по 2 часа, 50 000 тенге")
    assert "занятий" in out and "часа" in out and "тенге" in out


# ── What clients actually heard: the bot reading a payment URL out loud ──
# The link still reaches the client as text right after the voice note, so speech loses
# nothing by dropping it.


def test_tts_never_reads_a_url_aloud():
    out = normalize_for_tts("Я подготовила ссылку на оплату: https://example.kz/pay/ab12. Хотите?")
    assert "http" not in out and "ab12" not in out and "example" not in out
    assert out.startswith("Я подготовила ссылку на оплату. Хотите")


def test_tts_drops_www_links_and_emoji():
    out = normalize_for_tts("Прайс — www.example.kz/price 🙂 Что скажете?")
    assert "example" not in out and "🙂" not in out and "Что скажете?" in out


def test_chat_text_keeps_links_and_emoji():
    """Only SPEECH loses them — the client must still get a tappable link."""
    src = "Ссылка: https://example.kz/pay/ab12 🙂"
    assert strip_markdown(src) == src


def test_tts_leaves_clock_times_and_decimals_alone():
    """Spelling every digit turned "10:00–18:00" into "десять:ноль–восемнадцать:ноль";
    the TTS engine reads the raw time correctly (verified by a TTS→STT round trip)."""
    out = normalize_for_tts("Отгрузка 10:00–18:00, объём 2,5 куба, партии 5-10 поддонов.")
    assert "10:00–18:00" in out and "2,5 куба" in out and "5-10 поддонов" in out


def test_tts_declines_after_genitive_preposition():
    """ElevenLabs never declines — fed "от пять поддонов" it says exactly that."""
    if textfmt._n2w:
        assert "от пяти поддонов" in normalize_for_tts("Отгружаем от 5 поддонов")
        assert "восемь поддонов" in normalize_for_tts("осталось 8 поддонов")  # count stays


# ── Что клиенты слышали в диалоге с Алмагуль 03.08.2026: «двести шестнадцать тенге
# СЛЭШ шэ-тэ», «один МТРИ», «инфо СОБАКА кэ-зэ». Прайс всегда идёт «за штуку», поэтому
# каждый из этих символов встречается в почти любом ценовом ответе.


def test_tts_never_says_slash():
    out = normalize_for_tts("Цена 216 ₸/шт, доставка 5 000 ₸/т, и/или самовывоз")
    assert "/" not in out
    assert "за штуку" in out and "за тонну" in out


def test_tts_expands_pieces_with_correct_plural():
    if textfmt._n2w:
        # штука — женского рода: «один штука» / «два штуки» звучит как машинный перевод
        assert "одна штука" in normalize_for_tts("остался 1 шт")
        assert "две штуки" in normalize_for_tts("нужно 2 шт")
        assert "триста штук" in normalize_for_tts("поддон 300 шт")
    assert not re.search(r"\bшт\b", normalize_for_tts("поддон 300 шт."))


def test_tts_cubic_metres_are_not_read_as_mtri():
    """«1 м3» доходило до числового правила и склеивалось в «мтри»."""
    for src in ("объём 1 м3", "объём 1 м³", "объём 1 куб.м"):
        out = normalize_for_tts(src)
        assert "мтри" not in out and "м3" not in out
        assert "кубическ" in out
    assert "кубических метра" in normalize_for_tts("нужно 3 м3")


def test_tts_expands_kilograms():
    assert "килограмма" in normalize_for_tts("вес 3,2 кг")
    assert "килограммов" in normalize_for_tts("вес 25 кг")
    assert "кг" not in normalize_for_tts("вес 25 кг")


def test_tts_never_reads_an_email_aloud():
    out = normalize_for_tts("Пишите на info@example.kz — отвечу быстро")
    assert "@" not in out and "example" not in out
    assert "Пишите на" in out and "отвечу быстро" in out


def test_tts_declines_hundreds_after_preposition():
    """«от 216 ₸» звучало как «от двести шестнадцать» — таблица падежей была до 20."""
    if textfmt._n2w:
        out = normalize_for_tts("цены от 216 ₸ до 408 ₸")
        assert "от двухсот шестнадцати" in out
        assert "двести" not in out
        assert "от ста" in normalize_for_tts("от 100 тенге")
        assert "от сорока пяти" in normalize_for_tts("от 45 поддонов")


def test_tts_real_reply_to_almagul():
    """Живой ответ бота целиком — ни одного символа не должно остаться в озвучке."""
    out = normalize_for_tts(
        "По цене у нас есть разные варианты: от 216 ₸/шт до 408 ₸/шт. "
        "Поддон 300 шт, вес 3,2 кг/шт, объём 1 м3. Пишите на info@example.kz"
    )
    for symbol in ("/", "₸", "@"):
        assert symbol not in out, f"в озвучку просочилось {symbol!r}: {out}"
    for abbr in (r"\bм3\b", r"\bшт\b", r"\bкг\b"):
        assert not re.search(abbr, out), f"сокращение {abbr} не раскрыто: {out}"


def test_tts_keeps_sentence_stop_after_tenge_abbreviation():
    if textfmt._n2w:
        assert "тенге. Доставка" in normalize_for_tts("Цена 120 тг. Доставка бесплатно")
        assert "тенге и доставка" in normalize_for_tts("Цена 95 тг. и доставка бесплатно")
