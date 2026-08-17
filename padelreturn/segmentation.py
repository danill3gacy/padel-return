"""Сегментация спящих + контрольная группа.

Ключевое правило: "спящий" — не фиксированные 60 дней, а нарушение личного ритма.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from . import db
from .config import Config
from .features import top_partners
from .utils import parse_dt, stable_bucket

SEGMENT_TITLES = {
    "A": "Оборванный новичок",
    "B": "Осиротевший (ушёл партнёр)",
    "C": "Выпавший постоянный",
    "D": "Сменивший ритм",
    "E": "Сезонный (отложить)",
    "F": "Проблемный (no-show)",
}
SEGMENT_PRIORITY = ["C", "B", "A", "D", "F", "E"]


def is_sleeping(days_since_last, avg_interval, cfg: Config) -> bool:
    if days_since_last is None:
        return False
    threshold = cfg.sleeping_min_days
    if avg_interval:
        threshold = max(threshold, avg_interval * cfg.sleeping_interval_mult)
    return days_since_last > threshold


def _exclusion(row, conn, club_id: int, cfg: Config, as_of: datetime) -> str | None:
    if row["is_staff"]:
        return "сотрудник клуба"
    if not row["phone"]:
        return "нет телефона"
    if row["stop_list"]:
        return "стоп-лист"
    if not row["consent"]:
        return "нет согласия на рассылку"
    if (row["visits_total"] or 0) == 0:
        return "ни одного визита"
    recent = db.scalar(
        conn,
        """SELECT COUNT(*) FROM messages m
           WHERE m.contact_id=? AND m.direction='out' AND m.sent_at IS NOT NULL
             AND julianday(?) - julianday(m.sent_at) < ?""",
        (row["id"], as_of.isoformat(sep=" "), cfg.recent_contact_days),
    )
    if recent:
        return f"писали в последние {cfg.recent_contact_days} дн."
    return None


def _is_seasonal(conn, contact_id: int, cfg: Config, as_of: datetime) -> bool:
    """Активность сконцентрирована в 3 месяцах года и сейчас — не его сезон."""
    rows = db.all_rows(
        conn,
        "SELECT starts_at FROM bookings WHERE contact_id=? AND status='done'",
        (contact_id,),
    )
    if len(rows) < cfg.seasonal_min_visits:
        return False
    months: Counter[int] = Counter()
    for r in rows:
        d = parse_dt(r["starts_at"])
        if d:
            months[d.month] += 1
    top3 = [m for m, _ in months.most_common(3)]
    share = sum(months[m] for m in top3) / sum(months.values())
    return share >= 0.8 and as_of.month not in top3


def _slot_disappeared(conn, contact_id: int, usual_dow, usual_hour) -> bool:
    """Клуб перестал ставить слоты в привычное время клиента (или они всегда заняты)."""
    if usual_dow is None or usual_hour is None:
        return False
    cnt = db.scalar(
        conn,
        """SELECT COUNT(*) FROM bookings
           WHERE status='done'
             AND CAST(strftime('%w', starts_at) AS INTEGER) = ?
             AND CAST(strftime('%H', starts_at) AS INTEGER) = ?
             AND julianday('now') - julianday(starts_at) < 45""",
        (((usual_dow + 1) % 7), usual_hour),
    )
    return cnt == 0


def build(conn, club_id: int, campaign_id: int, cfg: Config, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now()
    rows = db.all_rows(
        conn,
        """SELECT c.id, c.name, c.phone, c.consent, c.stop_list, c.is_staff, c.level,
                  f.visits_total, f.revenue_total, f.days_since_last, f.avg_interval,
                  f.usual_dow, f.usual_hour, f.no_show_rate, f.main_coach, f.level_proxy,
                  f.last_visit, f.tournaments, f.lessons_share
           FROM contacts c LEFT JOIN features f ON f.contact_id = c.id
           WHERE c.club_id = ?""",
        (club_id,),
    )

    conn.execute("DELETE FROM segments WHERE campaign_id=?", (campaign_id,))
    counts: Counter[str] = Counter()

    for r in rows:
        excl = _exclusion(r, conn, club_id, cfg, as_of)
        if excl:
            conn.execute(
                """INSERT INTO segments (campaign_id, contact_id, segment, sleeping, excluded_reason)
                   VALUES (?,?,?,?,?)""",
                (campaign_id, r["id"], None, 0, excl),
            )
            counts["excluded"] += 1
            continue

        sleeping = is_sleeping(r["days_since_last"], r["avg_interval"], cfg)
        if not sleeping:
            conn.execute(
                """INSERT INTO segments (campaign_id, contact_id, segment, sleeping, excluded_reason)
                   VALUES (?,?,?,?,?)""",
                (campaign_id, r["id"], None, 0, "активный"),
            )
            counts["active"] += 1
            continue

        segment = _classify(conn, club_id, r, cfg, as_of)
        is_control = 1 if stable_bucket(f"{campaign_id}:{r['id']}", "control") < cfg.control_share else 0

        conn.execute(
            """INSERT INTO segments (campaign_id, contact_id, segment, sleeping, is_control)
               VALUES (?,?,?,?,?)""",
            (campaign_id, r["id"], segment, 1, is_control),
        )
        counts[segment] += 1
        if is_control:
            counts["control"] += 1

    conn.commit()
    stats = {
        "total": len(rows),
        "sleeping": sum(counts[s] for s in SEGMENT_TITLES),
        "control": counts["control"],
        "excluded": counts["excluded"],
        "active": counts["active"],
        **{s: counts[s] for s in SEGMENT_PRIORITY},
    }
    db.log(conn, "segmentation", "build", stats)
    conn.commit()
    return stats


def _classify(conn, club_id: int, r, cfg: Config, as_of: datetime) -> str:
    visits = r["visits_total"] or 0

    if (r["no_show_rate"] or 0) > cfg.no_show_problem_rate and visits >= 3:
        return "F"
    if _is_seasonal(conn, r["id"], cfg, as_of):
        return "E"

    # B: главный партнёр тоже пропал
    partners = top_partners(conn, club_id, r["id"], limit=2)
    for p in partners:
        if p["games_count"] >= 3 and is_sleeping(p["days_since_last"], p["avg_interval"], cfg):
            return "B"

    if visits <= cfg.newbie_max_visits:
        return "A"
    if visits >= cfg.loyal_min_visits:
        return "C"
    if _slot_disappeared(conn, r["id"], r["usual_dow"], r["usual_hour"]):
        return "D"
    return "C" if visits >= 5 else "A"


def audience(conn, campaign_id: int, include_control: bool = False) -> list:
    sql = """SELECT s.contact_id, s.segment, s.is_control, s.reason, s.confidence,
                    s.evidence, s.best_offer,
                    c.name, c.phone, c.tg_chat_id, c.gender,
                    f.visits_total, f.revenue_total, f.days_since_last, f.avg_interval,
                    f.usual_dow, f.usual_hour, f.last_visit, f.main_coach, f.level_proxy,
                    f.lessons_share, f.tournaments, f.no_show_rate
             FROM segments s
             JOIN contacts c ON c.id = s.contact_id
             LEFT JOIN features f ON f.contact_id = s.contact_id
             WHERE s.campaign_id=? AND s.sleeping=1 AND s.segment IS NOT NULL
               AND s.segment NOT IN ('E')"""
    if not include_control:
        sql += " AND s.is_control=0"
    sql += " ORDER BY CASE s.segment " + " ".join(
        f"WHEN '{s}' THEN {i}" for i, s in enumerate(SEGMENT_PRIORITY)
    ) + " END, f.revenue_total DESC"
    return db.all_rows(conn, sql, (campaign_id,))
