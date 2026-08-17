"""Расчёт признаков клиента и графа партнёрств."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime

from . import db
from .utils import iso, median, mode_or_none, parse_dt


def compute(conn: sqlite3.Connection, club_id: int, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now()
    contacts = db.all_rows(conn, "SELECT id, level FROM contacts WHERE club_id=?", (club_id,))
    bookings = db.all_rows(
        conn,
        """SELECT contact_id, starts_at, court_id, type, status, amount, coach_id
           FROM bookings WHERE club_id=? ORDER BY starts_at""",
        (club_id,),
    )

    by_client: dict[int, list] = defaultdict(list)
    for b in bookings:
        by_client[b["contact_id"]].append(b)

    conn.execute("DELETE FROM features WHERE club_id=?", (club_id,))

    for c in contacts:
        rows = by_client.get(c["id"], [])
        done = [r for r in rows if r["status"] == "done"]
        cancelled = [r for r in rows if r["status"] == "cancelled"]
        no_shows = [r for r in rows if r["status"] == "no_show"]

        parsed = (parse_dt(r["starts_at"]) for r in done if r["starts_at"])
        dts = sorted(d for d in parsed if d is not None)

        first_visit = dts[0] if dts else None
        last_visit = dts[-1] if dts else None
        visits = len(dts)
        revenue = sum(float(r["amount"] or 0) for r in done)
        days_since = (as_of - last_visit).days if last_visit else None
        intervals = [(dts[i] - dts[i - 1]).days for i in range(1, len(dts))]
        avg_interval = median(intervals) if intervals else None
        lifespan = (last_visit - first_visit).days if first_visit and last_visit else 0
        usual_dow = mode_or_none([d.weekday() for d in dts])
        usual_hour = mode_or_none([d.hour for d in dts])
        main_coach = mode_or_none([r["coach_id"] for r in done if r["coach_id"]])

        denom_ns = visits + len(no_shows)
        no_show_rate = len(no_shows) / denom_ns if denom_ns else 0.0
        cancel_rate = len(cancelled) / len(rows) if rows else 0.0
        lessons = len([r for r in done if r["type"] in ("lesson", "group")])
        lessons_share = lessons / visits if visits else 0.0
        tournaments = len([r for r in done if r["type"] == "tournament"])

        level_proxy = (
            c["level"]
            if c["level"] is not None
            else _level_proxy(visits, lessons_share, tournaments)
        )

        conn.execute(
            """INSERT INTO features (contact_id, club_id, first_visit, last_visit, visits_total,
               revenue_total, days_since_last, avg_interval, lifespan_days, usual_dow, usual_hour,
               main_coach, no_show_rate, cancel_rate, lessons_share, tournaments, level_proxy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                c["id"],
                club_id,
                iso(first_visit),
                iso(last_visit),
                visits,
                revenue,
                days_since,
                avg_interval,
                lifespan,
                usual_dow,
                usual_hour,
                main_coach,
                no_show_rate,
                cancel_rate,
                lessons_share,
                tournaments,
                level_proxy,
            ),
        )

    _compute_partnerships(conn, club_id, bookings)
    conn.commit()

    stats = {
        "contacts": len(contacts),
        "with_visits": db.scalar(
            conn, "SELECT COUNT(*) FROM features WHERE club_id=? AND visits_total>0", (club_id,)
        ),
        "partnerships": db.scalar(
            conn, "SELECT COUNT(*) FROM partnerships WHERE club_id=?", (club_id,)
        ),
    }
    db.log(conn, "features", "compute", stats)
    conn.commit()
    return stats


def _level_proxy(visits: int, lessons_share: float, tournaments: int) -> float:
    """Грубая оценка уровня по шкале 1-7, когда клуб не ведёт уровни."""
    base = 1.6
    base += min(visits, 40) * 0.055  # опыт
    base += min(tournaments, 10) * 0.09  # турниры — сильный сигнал
    base += lessons_share * 0.5  # занимался с тренером
    return round(min(base, 6.5), 2)


def _compute_partnerships(
    conn: sqlite3.Connection, club_id: int, bookings: list[sqlite3.Row]
) -> None:
    """Партнёрства восстанавливаем по совпадению корта и времени начала."""
    conn.execute("DELETE FROM partnerships WHERE club_id=?", (club_id,))
    slots: dict[tuple, list[tuple[int, str]]] = defaultdict(list)
    for b in bookings:
        if b["status"] != "done" or not b["court_id"] or not b["starts_at"]:
            continue
        slots[(b["court_id"], b["starts_at"])].append((b["contact_id"], b["starts_at"]))

    pairs: dict[tuple[int, int], list] = defaultdict(list)
    for _, members in slots.items():
        ids = sorted({m[0] for m in members})
        if len(ids) < 2 or len(ids) > 6:
            continue
        when = members[0][1]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs[(ids[i], ids[j])].append(when)

    for (left, right), whens in pairs.items():
        last_game = max(whens)
        for x, y in ((left, right), (right, left)):
            conn.execute(
                """INSERT OR REPLACE INTO partnerships
                       (club_id, contact_a, contact_b, games_count, last_game_at)
                   VALUES (?,?,?,?,?)""",
                (club_id, x, y, len(whens), last_game),
            )


def top_partners(conn: sqlite3.Connection, club_id: int, contact_id: int, limit: int = 3) -> list:
    return db.all_rows(
        conn,
        """SELECT p.contact_b AS id, p.games_count, p.last_game_at,
                  c.name, f.last_visit, f.days_since_last, f.avg_interval, f.level_proxy
           FROM partnerships p
           JOIN contacts c ON c.id = p.contact_b
           LEFT JOIN features f ON f.contact_id = p.contact_b
           WHERE p.club_id=? AND p.contact_a=?
           ORDER BY p.games_count DESC LIMIT ?""",
        (club_id, contact_id, limit),
    )
