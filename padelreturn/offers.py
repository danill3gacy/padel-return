"""Оффер-движок — сердце продукта.

Идея из PRD: не скидка, а снятая организационная нагрузка.
Пул кандидатов копится по ходу кампании, поэтому оффер усиливается сам:
"свободный слот" -> "уже двое вашего уровня, нужен третий" -> "собранная игра".
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import db
from .config import Config
from .utils import fmt_slot_ru

KIND_TITLES = {
    "assembled": "собранная игра",
    "assembling": "игра, которая собирается",
    "usual_slot": "слот в привычное время",
    "tournament": "турнир Американо",
    "beginner": "новичковая сессия с тренером",
    "offpeak": "офф-пик цена",
}

# Иерархия из PRD: цена — всегда последняя.
OFFER_BY_REASON = {
    "нет_партнёров": ["assembling", "assembled", "tournament"],
    "не_понравился_первый_опыт": ["beginner", "tournament"],
    "уровень_не_совпал": ["assembling", "tournament"],
    "ушёл_тренер": ["beginner", "assembling"],
    "сменился_график": ["usual_slot", "assembling"],
    "слишком_дорого": ["offpeak", "tournament"],
    "ушёл_в_другой_клуб": ["assembling", "usual_slot"],
    "травма_пауза": ["usual_slot", "tournament"],
    "неизвестно": ["assembling", "usual_slot", "tournament"],
}

SEGMENT_DEFAULT = {
    "A": ["tournament", "beginner", "assembling"],
    "B": ["assembling", "assembled", "tournament"],
    "C": ["usual_slot", "assembling"],
    "D": ["usual_slot", "offpeak", "assembling"],
    "F": ["usual_slot"],
}


def free_slots(conn, club_id: int, cfg: Config, horizon_days: int | None = None,
               as_of: datetime | None = None) -> list[dict]:
    """Свободные слоты на горизонте: часы работы минус занятые брони.

    В v1 нет интеграции с CRM, поэтому расписание считается по настройкам клуба
    (часы работы, список кортов) и известным броням.
    """
    as_of = as_of or datetime.now()
    horizon = horizon_days or cfg.offer_horizon_days
    st = db.club_settings(conn, club_id)
    open_h = int(st.get("open_hour", 7))
    close_h = int(st.get("close_hour", 23))
    courts = st.get("courts") or [f"C{i+1}" for i in range(int(st.get("courts_count", 4)))]
    peak = set(st.get("peak_hours", list(range(18, 23))))

    busy = set()
    rows = db.all_rows(
        conn,
        """SELECT court_id, starts_at FROM bookings
           WHERE club_id=? AND status IN ('done','planned') AND starts_at >= ?""",
        (club_id, as_of.isoformat(sep=" ")),
    )
    for r in rows:
        busy.add((r["court_id"], (r["starts_at"] or "")[:13]))

    slots = []
    for d in range(1, horizon + 1):
        day = (as_of + timedelta(days=d)).date()
        for hour in range(open_h, close_h):
            for court in courts:
                dt = datetime(day.year, day.month, day.day, hour)
                if (court, dt.isoformat(sep=" ")[:13]) in busy:
                    continue
                slots.append({
                    "datetime": dt,
                    "court_id": court,
                    "is_peak": hour in peak,
                    "price": float(st.get("price_peak", 4200) if hour in peak else st.get("price_offpeak", 2800)),
                })
    return slots


def _pick_slot(slots: list[dict], dow=None, hour=None, prefer_offpeak=True) -> dict | None:
    if not slots:
        return None
    if dow is not None and hour is not None:
        exact = [s for s in slots if s["datetime"].weekday() == dow and s["datetime"].hour == hour]
        if exact:
            return min(exact, key=lambda s: s["datetime"])
        near = [s for s in slots if s["datetime"].weekday() == dow and abs(s["datetime"].hour - hour) <= 1]
        if near:
            return min(near, key=lambda s: s["datetime"])
    pool = [s for s in slots if not s["is_peak"]] if prefer_offpeak else slots
    pool = pool or slots
    return min(pool, key=lambda s: s["datetime"])


def plan_offer(conn, club_id: int, campaign_id: int, row, cfg: Config,
               slots: list[dict], as_of: datetime | None = None) -> dict:
    """Подбирает оффер для конкретного человека. Сначала — присоединиться к живому пулу."""
    as_of = as_of or datetime.now()
    level = row["level_proxy"] or 3.0

    # 1. Есть ли уже собирающаяся игра его уровня?
    open_offer = db.one(
        conn,
        """SELECT * FROM offers
           WHERE campaign_id=? AND status='open' AND seats_filled < seats_total
             AND ? BETWEEN level_min AND level_max
             AND slot_datetime > ?
           ORDER BY seats_filled DESC, slot_datetime ASC LIMIT 1""",
        (campaign_id, level, as_of.isoformat(sep=" ")),
    )
    if open_offer and open_offer["seats_filled"] >= 1:
        kind = "assembled" if open_offer["seats_filled"] >= 3 else "assembling"
        conn.execute("UPDATE offers SET kind=? WHERE id=?", (kind, open_offer["id"]))
        conn.execute(
            "INSERT OR IGNORE INTO offer_seats (offer_id, contact_id, state) VALUES (?,?,'invited')",
            (open_offer["id"], row["contact_id"]),
        )
        conn.commit()
        return _offer_dict(conn, open_offer["id"])

    # 2. Иначе создаём новый под его сценарий
    preferred = OFFER_BY_REASON.get(row["reason"] or "неизвестно") or SEGMENT_DEFAULT.get(row["segment"], ["assembling"])
    if row["best_offer"] == "слот_в_привычное_время":
        preferred = ["usual_slot"] + preferred
    elif row["best_offer"] == "турнир_американо":
        preferred = ["tournament"] + preferred
    elif row["best_offer"] == "новичковая_сессия":
        preferred = ["beginner"] + preferred
    elif row["best_offer"] == "офф-пик_цена":
        preferred = ["offpeak"] + preferred

    kind = preferred[0]
    if kind == "usual_slot":
        slot = _pick_slot(slots, row["usual_dow"], row["usual_hour"], prefer_offpeak=False)
    elif kind == "tournament":
        slot = _pick_slot(slots, dow=6, hour=12, prefer_offpeak=False) or _pick_slot(slots)
    else:
        slot = _pick_slot(slots, row["usual_dow"], row["usual_hour"])

    if slot is None:
        slot = {"datetime": as_of + timedelta(days=3), "court_id": "C1", "is_peak": False, "price": 2800}

    seats = 8 if kind == "tournament" else cfg.seats_per_offer
    cur = conn.execute(
        """INSERT INTO offers (campaign_id, kind, slot_datetime, court_id, level_min, level_max,
                               seats_total, seats_filled, price_hint, status)
           VALUES (?,?,?,?,?,?,?,0,?, 'open')""",
        (campaign_id, kind, slot["datetime"].isoformat(sep=" "), slot["court_id"],
         round(level - cfg.level_window, 2), round(level + cfg.level_window, 2),
         seats, slot["price"]),
    )
    offer_id = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO offer_seats (offer_id, contact_id, state) VALUES (?,?,'invited')",
        (offer_id, row["contact_id"]),
    )
    conn.commit()
    return _offer_dict(conn, offer_id)


def _offer_dict(conn, offer_id: int) -> dict:
    o = db.must(conn, "SELECT * FROM offers WHERE id=?", (offer_id,))
    dt = datetime.fromisoformat(o["slot_datetime"])
    return {
        "id": o["id"],
        "kind": o["kind"],
        "kind_title": KIND_TITLES.get(o["kind"], o["kind"]),
        "datetime": dt,
        "when_ru": fmt_slot_ru(dt),
        "court_id": o["court_id"],
        "seats_total": o["seats_total"],
        "seats_filled": o["seats_filled"],
        "seats_left": o["seats_total"] - o["seats_filled"],
        "price": o["price_hint"],
        "level_min": o["level_min"],
        "level_max": o["level_max"],
    }


def accept(conn, offer_id: int, contact_id: int) -> dict:
    """Клиент согласился — занимаем место, пересчитываем статус оффера."""
    conn.execute(
        "INSERT OR REPLACE INTO offer_seats (offer_id, contact_id, state, updated_at) "
        "VALUES (?,?,'accepted', datetime('now'))",
        (offer_id, contact_id),
    )
    filled = db.scalar(
        conn, "SELECT COUNT(*) FROM offer_seats WHERE offer_id=? AND state='accepted'", (offer_id,)
    )
    o = db.must(conn, "SELECT seats_total FROM offers WHERE id=?", (offer_id,))
    status = "full" if filled >= o["seats_total"] else "open"
    conn.execute("UPDATE offers SET seats_filled=?, status=? WHERE id=?", (filled, status, offer_id))
    conn.commit()
    return _offer_dict(conn, offer_id)


def decline(conn, offer_id: int, contact_id: int) -> None:
    conn.execute(
        "UPDATE offer_seats SET state='declined', updated_at=datetime('now') "
        "WHERE offer_id=? AND contact_id=?",
        (offer_id, contact_id),
    )
    conn.commit()


def summary(conn, campaign_id: int) -> dict:
    rows = db.all_rows(
        conn,
        """SELECT kind, status, COUNT(*) n, SUM(seats_filled) filled
           FROM offers WHERE campaign_id=? GROUP BY kind, status""",
        (campaign_id,),
    )
    return {
        "by_kind": [dict(r) for r in rows],
        "full": db.scalar(conn, "SELECT COUNT(*) FROM offers WHERE campaign_id=? AND status='full'", (campaign_id,)),
        "open": db.scalar(conn, "SELECT COUNT(*) FROM offers WHERE campaign_id=? AND status='open'", (campaign_id,)),
        "seats_taken": db.scalar(
            conn,
            "SELECT COUNT(*) FROM offer_seats os JOIN offers o ON o.id=os.offer_id "
            "WHERE o.campaign_id=? AND os.state='accepted'",
            (campaign_id,),
        ),
    }
