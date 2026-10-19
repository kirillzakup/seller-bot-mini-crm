"""Clean the assistant's text for delivery.

Two levels:
- ``strip_markdown`` — for the CLIENT-FACING TEXT: drop markdown markers (``**``, ``*``,
  backticks, headings) that WhatsApp/Telegram show literally and that clutter the chat.
- ``normalize_for_tts`` — for the SPOKEN reply (ElevenLabs reads text literally): also
  expand currency ("тг"/"₸" → "тенге"), fix "с 0" → "с нуля", and drop quotes so the
  voice doesn't read «...» or say "тэ" / "ноль".
"""

from __future__ import annotations

import re

try:  # optional — spelled-out numbers make speech clear; degrade gracefully if missing
    from num2words import num2words as _n2w
except Exception:  # noqa: BLE001
    _n2w = None

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MULTISPACE = re.compile(r"[ \t]{2,}")
_TENGE_SYMBOL = re.compile(r"(?<=\d)\s*₸")
_TENGE_ABBR = re.compile(r"(?<=\d)\s*(?:тг|тнг|т)\b\.?", re.IGNORECASE)
# "50 000 тг. Я уже…" — that dot ends a SENTENCE, and eating it made the voice run both
# sentences together in one breath. Keep it when a capital letter or the end follows;
# elsewhere ("50 тг. и ещё") it is just the abbreviation dot and still goes.
# NB: no re.IGNORECASE here — it would make the "capital letter" lookahead match lowercase
# too, and "4 990 тг. и доступ…" would keep a bogus full stop mid-sentence.
_TENGE_ABBR_EOS = re.compile(
    r"(?<=\d)\s*(?:[тТ][гГ]|[тТ][нН][гГ]|[тТ])\b\.(?=\s+[А-ЯЁA-Z]|\s*$)"
)
_FROM_ZERO = re.compile(r"\bс\s*0\b", re.IGNORECASE)
_QUOTES = re.compile(r"[«»„“”\"]")

# TTS number/unit normalization — the model otherwise reads "250x60x65" as a random
# mix of Russian/English and stumbles on the "x" and the bare digits.
_DIM_SEP = re.compile(r"(?<=\d)\s*[xхXХ×*]\s*(?=\d)")  # 250x60 / 250х60 / 250×60 → " на "
_M2 = re.compile(r"(?<![А-Яа-яЁёA-Za-z])(?:м²|м2\b|кв\.?\s*м\b)", re.IGNORECASE)
_MM = re.compile(r"(?<=\d)\s*мм\b", re.IGNORECASE)
_SM = re.compile(r"(?<=\d)\s*см\b", re.IGNORECASE)
_PERCENT = re.compile(r"(?<=\d)\s*%")

# «216 ₸/шт» — the slash is read aloud as "слэш" and "шт" as "шэ-тэ". Prices are quoted
# per unit constantly, so this runs before the unit rules below and turns the whole
# construction into plain speech: "216 тенге за штуку".
_PER_UNIT = re.compile(
    r"\s*/\s*(шт\.?|штук[аи]?|м²|м3|м³|м2|кв\.?\s*м|куб\.?\s*м|кг|тонн[ауы]?|т"
    r"|поддон[ае]?|п\.?\s*м|пог\.?\s*м|мес\.?|день|сутки|час)(?![А-Яа-яЁёA-Za-z])",
    re.IGNORECASE,
)
_PER_UNIT_SPOKEN = {
    "шт": "штуку", "шт.": "штуку", "штука": "штуку", "штуки": "штуку", "штук": "штуку",
    "м2": "квадратный метр", "м²": "квадратный метр", "квм": "квадратный метр",
    "м3": "кубический метр", "м³": "кубический метр", "кубм": "кубический метр",
    "кг": "килограмм", "т": "тонну", "тонна": "тонну", "тонну": "тонну", "тонны": "тонну",
    "поддон": "поддон", "поддона": "поддон", "поддоне": "поддон",
    "пм": "погонный метр", "погм": "погонный метр",
    "мес": "месяц", "мес.": "месяц", "день": "день", "сутки": "сутки", "час": "час",
}
# Units that still carry a count — spoken form depends on the number ("2 штуки" / "5 штук").
_PCS = re.compile(r"(\d[\d  ]*)\s*шт\.?(?![А-Яа-яЁёA-Za-z])", re.IGNORECASE)
_KG = re.compile(r"(\d[\d  ]*(?:[.,]\d+)?)\s*кг\b", re.IGNORECASE)
# «объём 1 м3» used to survive every unit rule and reach the number pass, which spelled
# the "3" and glued it on: "один мтри". Cubic metres are how brick ships, so they matter.
_M3 = re.compile(
    r"(?:(\d[\d  ]*(?:[.,]\d+)?)\s*)?(?<![А-Яа-яЁёA-Za-z])(?:м³|м3\b|куб\.?\s*м\b)",
    re.IGNORECASE,
)
# A leftover slash anywhere else ("и/или") is silent-at-best, "слэш" at worst.
_SLASH = re.compile(r"\s*/\s*")
# Only BARE numbers get spelled out. Structured ones — clock times, decimals, dates,
# ranges — are matched first and handed to the TTS engine untouched: it reads "10:00–12:00"
# far better than we can, whereas spelling every digit turned it into the unusable
# "десять:ноль–двенадцать:ноль" (and "2,5 недели" into "два,пять недели").
_NUMBER = re.compile(
    r"\d+(?:[.,:]\d+)+"        # 10:00 · 2,5 · 23.07.2026 — leave alone
    r"|\d+\s*[–—-]\s*\d+"      # 7–17 лет — leave alone
    r"|\d[\d  ]*\d|\d"                # bare number (incl. space-grouped thousands) → words
)
_KEEP_AS_IS = ".,:–—-"  # a match containing any of these is a structured number, not a bare one

# A spoken reply must NEVER contain a URL: ElevenLabs reads it out piece by piece
# ("аш-тэ-тэ-пэ-эс двоеточие слэш слэш…") — which is exactly what clients heard on every
# payment link. The link still reaches them as TEXT right after the voice note (see
# ``_send_reply``), so dropping it from speech loses nothing. A leading colon or dash goes
# with it, so "ссылку на оплату: https://…" reads as "ссылку на оплату."
# The trailing char class stops before ".", "," etc. so sentence punctuation survives.
_URL = re.compile(r"\s*[:\-–—]?\s*(?:https?://|www\.)[^\s]*[^\s.,;:!?)\]]", re.IGNORECASE)
# Same reasoning for an e-mail: spoken, "info@example.kz" becomes "инфо собака экзампл точка
# кэ-зэ". It reaches the client as text in the same reply, so speech loses nothing.
_EMAIL = re.compile(r"\s*[:\-–—]?\s*[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
# Emoji are silent at best and vocalised as noise at worst — strip them from SPEECH only
# (the chat text keeps them: they are half the bot's warmth).
_EMOJI = re.compile("[\U0001f000-\U0001faff←-⇿⌀-➿⬀-⯿️]+")


# ElevenLabs pronounces exactly what it reads: fed "от 6 лет" it says «от ШЕСТЬ лет», and
# fed the word "шесть" it says the same — verified with a TTS→STT round trip. It will never
# decline for us, so after a genitive-taking preposition we must write the genitive form
# ourselves. num2words only ever returns the nominative, hence this small table.
_GEN_UNITS = {
    1: "одного", 2: "двух", 3: "трёх", 4: "четырёх", 5: "пяти",
    6: "шести", 7: "семи", 8: "восьми", 9: "девяти",
}
_GEN_TEENS = {
    10: "десяти", 11: "одиннадцати", 12: "двенадцати", 13: "тринадцати",
    14: "четырнадцати", 15: "пятнадцати", 16: "шестнадцати", 17: "семнадцати",
    18: "восемнадцати", 19: "девятнадцати",
}
_GEN_TENS = {
    20: "двадцати", 30: "тридцати", 40: "сорока", 50: "пятидесяти",
    60: "шестидесяти", 70: "семидесяти", 80: "восьмидесяти", 90: "девяноста",
}
_GEN_HUNDREDS = {
    100: "ста", 200: "двухсот", 300: "трёхсот", 400: "четырёхсот", 500: "пятисот",
    600: "шестисот", 700: "семисот", 800: "восьмисот", 900: "девятисот",
}
_GEN_PREP = re.compile(r"\b(?:от|до|с|около|свыше|более|менее)\s+$", re.IGNORECASE)


def _genitive(n: int) -> str | None:
    """Genitive form of an integer below 1000, composed from the tables above.

    Prices live in the hundreds ("от 216 ₸"), so a flat 1–20 lookup left the most
    common case undeclined: «от двести шестнадцать тенге». Four digits and up stay
    nominative — rare after these prepositions, and the composition rules differ.
    """
    if n <= 0 or n >= 1000:
        return None
    parts = []
    if hundreds := n // 100 * 100:
        parts.append(_GEN_HUNDREDS[hundreds])
    rest = n % 100
    if 10 <= rest <= 19:
        parts.append(_GEN_TEENS[rest])
    else:
        if tens := rest // 10 * 10:
            parts.append(_GEN_TENS[tens])
        if units := rest % 10:
            parts.append(_GEN_UNITS[units])
    return " ".join(parts)


def _plural(raw: str, one: str, few: str, many: str) -> str:
    """Pick the Russian plural form for the count that precedes a unit.

    "1 шт" → штука, "2 шт" → штуки, "5 шт" → штук. Decimals take the "few" form
    ("3,2 килограмма"), which is what Russian does with fractional counts.
    """
    digits = re.sub(r"[  ]", "", raw or "")
    if "," in digits or "." in digits:
        return few
    try:
        n = int(digits)
    except ValueError:
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _spell_number(m: re.Match) -> str:
    token = m.group(0)
    if any(c in token for c in _KEEP_AS_IS):  # time/decimal/date/range → TTS knows better
        return token
    digits = re.sub(r"[  ]", "", token)
    if not _n2w or len(digits) > 9:  # no lib, or absurdly long (id/phone) → leave as digits
        return m.group(0)
    # «от 6 лет» → «от шести лет» (the engine cannot decline; see _GENITIVE)
    if _GEN_PREP.search(m.string[: m.start()]):
        genitive = _genitive(int(digits))
        if genitive:
            return genitive
    try:
        return _n2w(int(digits), lang="ru")
    except Exception:  # noqa: BLE001
        return m.group(0)


def _spell_per_unit(m: re.Match) -> str:
    """«/шт» → « за штуку». Falls back to the raw unit if we have no spoken form."""
    key = re.sub(r"[\s.]", "", m.group(1)).lower()
    return " за " + _PER_UNIT_SPOKEN.get(key, m.group(1))


_FEMININE = ((re.compile(r"\bодин$"), "одна"), (re.compile(r"\bдва$"), "две"))


def _spell_pcs(m: re.Match) -> str:
    """«300 шт» → «триста штук». Штука женского рода, поэтому счёт спеллим здесь же:
    иначе числовое правило даст мужской род — «один штука», «два штуки»."""
    count, form = m.group(1), _plural(m.group(1), "штука", "штуки", "штук")
    digits = re.sub(r"[  ]", "", count)
    if _n2w and digits.isdigit() and len(digits) <= 9:
        try:
            count = _n2w(int(digits), lang="ru")
        except Exception:  # noqa: BLE001 — падать из-за озвучки нельзя, оставим цифры
            pass
        else:
            for pattern, feminine in _FEMININE:
                count = pattern.sub(feminine, count)
    return f"{count} {form}"


def _spell_kg(m: re.Match) -> str:
    return f"{m.group(1)} " + _plural(m.group(1), "килограмм", "килограмма", "килограммов")


def _spell_m3(m: re.Match) -> str:
    count = m.group(1)
    if not count:
        return " кубических метров"
    form = _plural(count, "кубический метр", "кубических метра", "кубических метров")
    return f"{count} {form}"


def strip_markdown(text: str) -> str:
    """Remove markdown formatting so the chat text is clean (no literal ``**`` etc.)."""
    t = text or ""
    t = t.replace("*", "").replace("`", "")
    t = _HEADING.sub("", t)
    t = _MULTISPACE.sub(" ", t)
    return t.strip()


def normalize_for_tts(text: str) -> str:
    """Prepare text so ElevenLabs reads it correctly aloud.

    Besides currency/quote cleanup, spell out dimensions and numbers so the model
    doesn't garble "коробка 250x60x65" into a Russian/English mush — it becomes
    "коробка двести пятьдесят на шестьдесят на шестьдесят пять".
    """
    # URLs and e-mails go FIRST — their own digits/"x"/dots would otherwise be spelled out.
    t = _URL.sub("", text or "")            # never read a payment link out loud
    t = _EMAIL.sub("", t)                   # nor an address, letter by letter
    # Dimensions BEFORE markdown strip — otherwise strip_markdown eats the "*" in "250*60".
    t = _DIM_SEP.sub(" на ", t)    # 250x60x65 → 250 на 60 на 65
    t = strip_markdown(t)
    t = _FROM_ZERO.sub("с нуля", t)         # «Нейросети с 0» → «Нейросети с нуля»
    t = _PER_UNIT.sub(_spell_per_unit, t)   # 216 ₸/шт → 216 ₸ за штуку
    t = _M2.sub(" квадратных метров", t)    # 120 м² → 120 квадратных метров
    t = _M3.sub(_spell_m3, t)               # 1 м3 → 1 кубический метр (было «мтри»)
    t = _MM.sub(" миллиметров", t)          # 65 мм → 65 миллиметров
    t = _SM.sub(" сантиметров", t)
    t = _KG.sub(_spell_kg, t)               # 3,2 кг → 3,2 килограмма
    t = _PCS.sub(_spell_pcs, t)             # 300 шт → 300 штук
    t = _PERCENT.sub(" процентов", t)       # 10% → 10 процентов
    t = _TENGE_SYMBOL.sub(" тенге", t)      # "50 000₸" → "50 000 тенге"
    t = _TENGE_ABBR_EOS.sub(" тенге.", t)   # "50 000 тг. Я…" → keep the sentence stop
    t = _TENGE_ABBR.sub(" тенге", t)        # "50 000 тг" / "8 т" → "… тенге"
    t = _QUOTES.sub("", t)                  # drop quotes (voice reads them oddly)
    t = _EMOJI.sub("", t)                   # emoji have no pronunciation
    t = _SLASH.sub(" ", t)                  # any slash left would be read as «слэш»
    t = _NUMBER.sub(_spell_number, t)       # digits → Russian words (last, so units attach)
    t = _MULTISPACE.sub(" ", t)
    return t.strip()
