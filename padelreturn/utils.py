"""Мелкие утилиты: телефоны, даты, имена, статистика."""

from __future__ import annotations

import hashlib
import re
import statistics
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

RU_MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}
RU_DOW = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}
RU_DOW_PREP = {
    0: "в понедельник",
    1: "во вторник",
    2: "в среду",
    3: "в четверг",
    4: "в пятницу",
    5: "в субботу",
    6: "в воскресенье",
}


def normalize_phone(raw: str | None) -> str | None:
    """Приводим к E.164 для России. Возвращаем None, если это не телефон."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("9"):
        return None
    if len(digits) != 11 or not digits.startswith("7"):
        # иностранные номера пропускаем как есть, если длина правдоподобная
        if 10 <= len(digits) <= 15:
            return "+" + digits
        return None
    return "+" + digits


def parse_dt(value: object) -> datetime | None:
    """Терпимый парсер дат из выгрузок CRM."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip().replace("T", " ").replace("/", ".")
    if s.endswith("Z"):
        s = s[:-1]
    fmts = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%d.%m.%y %H:%M",
        "%d.%m.%y",
    )
    for f in fmts:
        try:
            return datetime.strptime(s[: len(f) + 6], f)
        except ValueError:
            continue
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def parse_money(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    if s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return 0.0


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(sep=" ", timespec="seconds") if dt else None


def fmt_date_ru(dt: datetime) -> str:
    return f"{dt.day} {RU_MONTHS[dt.month]}"


def fmt_slot_ru(dt: datetime) -> str:
    """'в четверг, 12 сентября, в 19:00'"""
    return f"{RU_DOW_PREP[dt.weekday()]}, {fmt_date_ru(dt)}, в {dt.strftime('%H:%M')}"


def first_name(full: str | None) -> str:
    if not full:
        return ""
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if not parts:
        return ""
    # В выгрузках чаще "Имя Фамилия", реже "Фамилия Имя". Берём первое слово.
    name = parts[0]
    return name[:1].upper() + name[1:]


def median(values: Iterable[float]) -> float:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else 0.0


def mode_or_none(values: Iterable) -> Any:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    try:
        return statistics.mode(vals)
    except statistics.StatisticsError:
        return vals[0]


def stable_bucket(key: str, salt: str = "") -> float:
    """Детерминированное число 0..1 — для контрольной группы без random."""
    h = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def days_between(a: datetime, b: datetime) -> int:
    return abs((b - a).days)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def next_weekday_at(base: datetime, weekday: int, hour: int, min_days_ahead: int = 1) -> datetime:
    """Ближайший будущий слот с нужным днём недели и часом."""
    d = base.date() + timedelta(days=min_days_ahead)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, hour, 0)
