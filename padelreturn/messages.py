"""Генерация сообщений. LLM опционально, шаблоны — всегда.

Правила тона из PRD: пишет администратор от первого лица, 2-4 предложения,
без эмодзи, без восклицательных знаков, без слов "акция/скидка/уникальный",
причина ухода прямо не называется, в конце — один конкретный вопрос.
"""

from __future__ import annotations

import random
import re
import sqlite3

from .config import Config
from .llm import LLM
from .utils import first_name

BANNED = [
    "акци",
    "спецпредложен",
    "уникальн",
    "спешите",
    "не упустите",
    "только сегодня",
    "выгодное предложение",
    "дорогой клиент",
    "уважаемый клиент",
    "мы соскучились",
]
BANNED_CHARS = "!"

SYSTEM = (
    "Ты администратор падел-клуба. Пишешь короткие личные сообщения клиентам в мессенджер. "
    "Пишешь как живой человек, а не как рассылка."
)

PROMPT = """Напиши личное сообщение клиенту, который давно не приходил в клуб «{club}».

О клиенте: {name}, был у нас {visits} раз, обычно играл {usual}, последний раз {last_visit}.
Вероятная причина паузы (не упоминай её прямо): {reason}.
Предложение, которое нужно передать: {offer_text}

Требования:
- 2-4 предложения, живой разговорный русский
- от первого лица, как пишет живой администратор
- НЕ упоминай, что заметил его отсутствие или анализировал историю посещений
- НЕ используй слова: акция, спецпредложение, уникальный, спешите, скидка
- без эмодзи и восклицательных знаков
- закончи одним конкретным вопросом, на который можно ответить "да"
- предложение должно быть конкретным: когда, во сколько, кто играет

Верни только текст сообщения, без кавычек и пояснений."""


def offer_text(offer: dict, club_name: str) -> str:
    """Человеческое описание оффера — идёт и в промпт, и в шаблон."""
    k = offer["kind"]
    when = offer["when_ru"]
    filled = offer["seats_filled"]
    if k == "assembled":
        return f"{when} собралась игра, свободно одно место, партнёры примерно вашего уровня"
    if k == "assembling":
        if filled >= 1:
            who = "двое" if filled == 2 else ("один игрок" if filled == 1 else f"{filled} игрока")
            need = "третий" if filled == 2 else ("второй" if filled == 1 else "ещё игроки")
            return f"{when} собирается игра, {who} примерно вашего уровня уже есть, нужен {need}"
        return f"{when} собирается игра для игроков вашего уровня"
    if k == "usual_slot":
        return f"{when} свободен корт в ваше обычное время"
    if k == "tournament":
        return (
            f"{when} проводим Американо — пары меняются каждые несколько геймов, "
            "поэтому приходить одному нормально"
        )
    if k == "beginner":
        return f"{when} есть занятие для начинающих с тренером, небольшая группа"
    if k == "offpeak":
        return f"{when} дневной корт по будничной цене — {int(offer['price'])} ₽ за час на четверых"
    return f"{when} свободен корт"


TEMPLATES = {
    "assembled": [
        "{name}, здравствуйте. {offer}. Могу записать вас. Играете?",
        "{name}, добрый день. {offer}. Забронировать за вами место?",
    ],
    "assembling": [
        "{name}, здравствуйте. {offer}. Могу вас записать. Играете?",
        "{name}, добрый день. {offer}. Поставить вас в состав?",
        "{name}, здравствуйте. {offer}. Забронировать вам место?",
    ],
    "usual_slot": [
        "{name}, здравствуйте. {offer}. Придержать корт за вами?",
        "{name}, добрый день. {offer}. Забронировать?",
        "{name}, здравствуйте. {offer}. Поставить бронь на вас?",
    ],
    "tournament": [
        "{name}, здравствуйте. {offer}. Поставить вас в список?",
        "{name}, добрый день. {offer}. Записать вас?",
    ],
    "beginner": [
        "{name}, здравствуйте. {offer}. Записать вас?",
        "{name}, добрый день. {offer}. Оставить за вами место?",
    ],
    "offpeak": [
        "{name}, здравствуйте. {offer}. Забронировать?",
        "{name}, добрый день. {offer}. Придержать корт?",
    ],
}

# Второе касание — другой формат, а не повтор.
TOUCH2 = {
    "default": [
        "{name}, здравствуйте. Ещё вариант: {offer}. Подойдёт?",
        "{name}, добрый день. Если то время не подошло — {offer}. Записать?",
    ],
}

TOUCH3 = [
    "{name}, здравствуйте. Если сейчас не до падела — напишите, уберу вас из рассылки "
    "и больше не буду беспокоить. А если захотите вернуться, просто напишите мне.",
]

FOOTER = "\n\nЕсли не хотите получать такие сообщения — ответьте «стоп»."


def sanitize(text: str) -> str:
    text = (text or "").strip().strip('"«»')
    text = re.sub(r"\s+", " ", text)
    for ch in BANNED_CHARS:
        text = text.replace(ch, ".")
    text = re.sub(r"\.{2,}", ".", text)
    return text.strip()


def violates(text: str) -> str | None:
    low = text.lower()
    for w in BANNED:
        if w in low:
            return f"запрещённое слово: {w}"
    if "!" in text:
        return "восклицательный знак"
    if len(text) > 480:
        return "слишком длинное"
    if len(text) < 30:
        return "слишком короткое"
    if not text.rstrip().endswith("?") and "стоп" not in low:
        return "нет вопроса в конце"
    return None


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def render_template(
    row: sqlite3.Row, offer: dict, club_name: str, touch: int, rnd: random.Random
) -> str:
    name = first_name(row["name"]) or "Здравствуйте"
    otext = offer_text(offer, club_name)
    if touch == 3:
        return rnd.choice(TOUCH3).format(name=name)
    if touch == 2:
        return rnd.choice(TOUCH2["default"]).format(name=name, offer=lower_first(otext))
    pool = TEMPLATES.get(offer["kind"], TEMPLATES["assembling"])
    return rnd.choice(pool).format(name=name, offer=cap(otext))


def generate(
    row: sqlite3.Row,
    offer: dict,
    club_name: str,
    cfg: Config,
    touch: int = 1,
    llm: LLM | None = None,
    seed: int | None = None,
) -> tuple[str, str]:
    """Возвращает (текст, источник). Источник: llm | template | template_fallback."""
    rnd = random.Random(seed if seed is not None else row["contact_id"] * 31 + touch)
    fallback = render_template(row, offer, club_name, touch, rnd)

    if touch == 3 or llm is None or not llm.enabled:
        return fallback, "template"

    from .utils import RU_DOW

    usual = "—"
    if row["usual_dow"] is not None and row["usual_hour"] is not None:
        usual = f"по {RU_DOW[row['usual_dow']]}м в {row['usual_hour']}:00"

    prompt = PROMPT.format(
        club=club_name,
        name=first_name(row["name"]),
        visits=row["visits_total"],
        usual=usual,
        last_visit=(row["last_visit"] or "")[:10],
        reason=row["reason"] or "неизвестно",
        offer_text=offer_text(offer, club_name),
    )
    raw = llm.complete(prompt, system=SYSTEM, max_tokens=300)
    if not raw:
        return fallback, "template_fallback"
    text = sanitize(raw)
    if violates(text):
        return fallback, "template_fallback"
    return text, "llm"


def with_footer(text: str, touch: int) -> str:
    return text + (FOOTER if touch == 1 else "")
