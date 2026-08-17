"""Импорт выгрузок CRM. Универсального парсера нет — есть автоугадывание + маппинг полей."""
from __future__ import annotations

import csv
import json
from typing import Iterable

from . import db
from .utils import normalize_phone, parse_dt, parse_money, iso

# Синонимы колонок по разным CRM (Sport Priority, Cue, Sport Groups, YCLIENTS, 1С, Fitbase).
CLIENT_ALIASES = {
    "external_id": ["client_id", "id", "клиент id", "идентификатор", "код клиента", "guid", "uid"],
    "name": ["name", "имя", "фио", "клиент", "полное имя", "full_name", "фамилия имя"],
    "phone": ["phone", "телефон", "тел", "мобильный", "номер", "phone_number", "контакт"],
    "created_at": ["created_at", "дата регистрации", "создан", "первый визит", "registered", "дата создания"],
    "level": ["level", "уровень", "рейтинг", "rating"],
    "gender": ["gender", "пол", "sex"],
    "consent": ["consent", "согласие", "consent_marketing", "рассылка", "подписка"],
    "tags": ["tags", "теги", "метки", "категория"],
    "is_staff": ["is_staff", "сотрудник", "staff", "тренер"],
}

BOOKING_ALIASES = {
    "external_id": ["booking_id", "id", "номер", "код брони", "заказ"],
    "contact_external_id": ["client_id", "клиент", "клиент id", "contact_id", "код клиента", "customer_id"],
    "starts_at": ["starts_at", "start", "начало", "дата начала", "дата и время", "дата", "datetime"],
    "ends_at": ["ends_at", "end", "конец", "окончание", "дата окончания"],
    "court_id": ["court_id", "корт", "court", "площадка", "ресурс"],
    "type": ["type", "тип", "услуга", "вид", "категория"],
    "status": ["status", "статус", "состояние"],
    "amount": ["amount", "сумма", "стоимость", "цена", "оплачено", "итого"],
    "coach_id": ["coach_id", "тренер", "coach", "инструктор"],
    "partners": ["partners", "участники", "игроки", "партнеры", "партнёры"],
}

STATUS_MAP = {
    "состоялась": "done", "завершена": "done", "оплачена": "done", "выполнена": "done",
    "done": "done", "completed": "done", "confirmed": "done", "visited": "done", "пришёл": "done",
    "отменена": "cancelled", "отмена": "cancelled", "cancelled": "cancelled", "canceled": "cancelled",
    "неявка": "no_show", "не пришёл": "no_show", "no_show": "no_show", "noshow": "no_show",
}

TYPE_MAP = {
    "аренда": "rent", "корт": "rent", "игра": "rent", "rent": "rent", "booking": "rent",
    "тренировка": "lesson", "занятие": "lesson", "персональная": "lesson", "lesson": "lesson",
    "групповая": "group", "группа": "group", "group": "group",
    "турнир": "tournament", "tournament": "tournament", "американо": "tournament",
}


def _norm_header(h: str) -> str:
    return (h or "").strip().lower().replace("ё", "е").replace("_", " ").replace("-", " ")


def guess_map(headers: Iterable[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    """Автоугадывание колонок. Возвращает {наше_поле: исходная_колонка}."""
    result: dict[str, str] = {}
    norm = {_norm_header(h): h for h in headers}
    for field, opts in aliases.items():
        for opt in opts:
            key = _norm_header(opt)
            if key in norm:
                result[field] = norm[key]
                break
        else:
            for nh, orig in norm.items():
                if any(_norm_header(o) in nh for o in opts):
                    result[field] = orig
                    break
    return result


def sniff_csv(path: str):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(f, dialect=dialect)
        return list(reader), reader.fieldnames or []


def save_map(conn, club_id: int, kind: str, mapping: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO field_maps (club_id, kind, map_json) VALUES (?,?,?)",
        (club_id, kind, json.dumps(mapping, ensure_ascii=False)),
    )
    conn.commit()


def load_map(conn, club_id: int, kind: str) -> dict | None:
    row = db.one(conn, "SELECT map_json FROM field_maps WHERE club_id=? AND kind=?", (club_id, kind))
    return json.loads(row["map_json"]) if row else None


def import_clients(conn, club_id: int, path: str, mapping: dict | None = None) -> dict:
    rows, headers = sniff_csv(path)
    mapping = mapping or load_map(conn, club_id, "clients") or guess_map(headers, CLIENT_ALIASES)
    save_map(conn, club_id, "clients", mapping)

    stats = {"rows": len(rows), "inserted": 0, "updated": 0, "no_phone": 0, "dup_phone": 0}
    seen_phones: dict[str, str] = {}

    for i, r in enumerate(rows):
        ext = str(r.get(mapping.get("external_id", ""), "") or "").strip() or f"row{i}"
        name = (r.get(mapping.get("name", ""), "") or "").strip()
        phone = normalize_phone(r.get(mapping.get("phone", "")))
        if not phone:
            stats["no_phone"] += 1
        elif phone in seen_phones:
            stats["dup_phone"] += 1
            continue
        else:
            seen_phones[phone] = ext

        created = parse_dt(r.get(mapping.get("created_at", "")))
        level_raw = r.get(mapping.get("level", ""))
        try:
            level = float(str(level_raw).replace(",", ".")) if level_raw else None
        except ValueError:
            level = None
        gender_raw = (r.get(mapping.get("gender", ""), "") or "").strip().lower()
        gender = "f" if gender_raw[:1] in ("ж", "f", "w") else ("m" if gender_raw[:1] in ("м", "m") else None)

        consent_raw = str(r.get(mapping.get("consent", ""), "") or "").strip().lower()
        consent = 0 if consent_raw in ("0", "нет", "no", "false", "отказ") else 1

        staff_raw = str(r.get(mapping.get("is_staff", ""), "") or "").strip().lower()
        is_staff = 1 if staff_raw in ("1", "да", "yes", "true", "тренер", "сотрудник") else 0

        exists = db.one(conn, "SELECT id FROM contacts WHERE club_id=? AND external_id=?", (club_id, ext))
        if exists:
            conn.execute(
                """UPDATE contacts SET name=?, phone=?, level=?, gender=?, consent=?,
                   is_staff=?, created_at=? WHERE id=?""",
                (name, phone, level, gender, consent, is_staff, iso(created), exists["id"]),
            )
            stats["updated"] += 1
        else:
            conn.execute(
                """INSERT INTO contacts (club_id, external_id, name, phone, level, gender,
                   consent, is_staff, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (club_id, ext, name, phone, level, gender, consent, is_staff, iso(created)),
            )
            stats["inserted"] += 1
    conn.commit()
    db.log(conn, "importer", "import_clients", stats)
    conn.commit()
    return stats


def import_bookings(conn, club_id: int, path: str, mapping: dict | None = None) -> dict:
    rows, headers = sniff_csv(path)
    mapping = mapping or load_map(conn, club_id, "bookings") or guess_map(headers, BOOKING_ALIASES)
    save_map(conn, club_id, "bookings", mapping)

    id_index = {
        r["external_id"]: r["id"]
        for r in db.all_rows(conn, "SELECT id, external_id FROM contacts WHERE club_id=?", (club_id,))
    }

    stats = {"rows": len(rows), "inserted": 0, "skipped_no_client": 0, "skipped_no_date": 0}
    for i, r in enumerate(rows):
        ext = str(r.get(mapping.get("external_id", ""), "") or "").strip() or f"b{i}"
        cext = str(r.get(mapping.get("contact_external_id", ""), "") or "").strip()
        contact_id = id_index.get(cext)
        if not contact_id:
            stats["skipped_no_client"] += 1
            continue
        starts = parse_dt(r.get(mapping.get("starts_at", "")))
        if not starts:
            stats["skipped_no_date"] += 1
            continue
        ends = parse_dt(r.get(mapping.get("ends_at", "")))
        status_raw = (r.get(mapping.get("status", ""), "") or "done").strip().lower()
        status = STATUS_MAP.get(status_raw, "done" if not status_raw else status_raw)
        type_raw = (r.get(mapping.get("type", ""), "") or "rent").strip().lower()
        btype = TYPE_MAP.get(type_raw, "rent")
        amount = parse_money(r.get(mapping.get("amount", "")))
        court = str(r.get(mapping.get("court_id", ""), "") or "").strip() or None
        coach = str(r.get(mapping.get("coach_id", ""), "") or "").strip() or None

        try:
            conn.execute(
                """INSERT OR IGNORE INTO bookings
                   (club_id, external_id, contact_id, starts_at, ends_at, court_id, type, status, amount, coach_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (club_id, ext, contact_id, iso(starts), iso(ends), court, btype, status, amount, coach),
            )
            stats["inserted"] += 1
        except Exception:
            pass
    conn.commit()
    db.log(conn, "importer", "import_bookings", stats)
    conn.commit()
    return stats
