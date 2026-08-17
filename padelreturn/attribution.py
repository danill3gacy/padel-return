"""Атрибуция: кто вернулся, сколько принёс, и какой из этого честный uplift.

Правило из PRD: возврат = состоявшаяся бронь в течение 21 дня после первого касания
у того, кто был sleeping. Деньги считаем за 60 дней. Платит клуб только за прирост
над контрольной группой.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import db
from .config import Config
from .utils import iso, parse_dt


def compute(
    conn: sqlite3.Connection,
    club_id: int,
    campaign_id: int,
    cfg: Config,
    as_of: datetime | None = None,
) -> dict:
    as_of = as_of or datetime.now()
    conn.execute("DELETE FROM attributions WHERE campaign_id=?", (campaign_id,))

    camp = db.must(conn, "SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    camp_start = parse_dt(camp["started_at"]) or as_of

    rows = db.all_rows(
        conn,
        """SELECT s.contact_id, s.is_control, s.segment,
                  (SELECT MIN(sent_at) FROM messages m
                   WHERE m.campaign_id=s.campaign_id AND m.contact_id=s.contact_id
                     AND m.direction='out' AND m.status='sent') AS first_touch
           FROM segments s
           WHERE s.campaign_id=? AND s.sleeping=1 AND s.segment IS NOT NULL AND s.segment<>'E'""",
        (campaign_id,),
    )

    for r in rows:
        # Для контрольной группы точкой отсчёта служит старт кампании.
        anchor = parse_dt(r["first_touch"]) or camp_start
        if r["is_control"]:
            anchor = camp_start
        elif r["first_touch"] is None:
            continue

        ret_end = anchor + timedelta(days=cfg.return_window_days)
        rev_end = anchor + timedelta(days=cfg.revenue_window_days)

        first_back = db.one(
            conn,
            """SELECT MIN(starts_at) AS d FROM bookings
               WHERE contact_id=? AND status='done' AND starts_at >= ? AND starts_at <= ?""",
            (r["contact_id"], iso(anchor), iso(ret_end)),
        )
        returned_at = first_back["d"] if first_back and first_back["d"] else None

        revenue = 0.0
        n_bookings = 0
        if returned_at:
            agg = db.must(
                conn,
                """SELECT COALESCE(SUM(amount),0) AS rev, COUNT(*) AS n FROM bookings
                   WHERE contact_id=? AND status='done' AND starts_at >= ? AND starts_at <= ?""",
                (r["contact_id"], returned_at, iso(rev_end)),
            )
            revenue = float(agg["rev"] or 0)
            n_bookings = int(agg["n"] or 0)

        conn.execute(
            """INSERT INTO attributions (campaign_id, contact_id, is_control, first_touch_at,
                   returned_at, revenue_60d, bookings_60d, second_booking)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                campaign_id,
                r["contact_id"],
                r["is_control"],
                iso(anchor),
                returned_at,
                revenue,
                n_bookings,
                1 if n_bookings >= 2 else 0,
            ),
        )

    conn.commit()
    return report(conn, campaign_id, cfg)


def report(conn: sqlite3.Connection, campaign_id: int, cfg: Config) -> dict:
    def agg(is_control: int) -> dict:
        row = db.must(
            conn,
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN returned_at IS NOT NULL THEN 1 ELSE 0 END) returned,
                      COALESCE(SUM(revenue_60d),0) revenue,
                      SUM(second_booking) second
               FROM attributions WHERE campaign_id=? AND is_control=?""",
            (campaign_id, is_control),
        )
        n = row["n"] or 0
        returned = row["returned"] or 0
        return {
            "n": n,
            "returned": returned,
            "rate": round(returned / n * 100, 1) if n else 0.0,
            "revenue": round(row["revenue"] or 0, 2),
            "second_booking": row["second"] or 0,
            "second_share": round((row["second"] or 0) / returned * 100, 1) if returned else 0.0,
        }

    treated = agg(0)
    control = agg(1)
    uplift_pp = round(treated["rate"] - control["rate"], 1)

    # Инкрементальная выручка: сколько принёс бы контроль на объёме аудитории.
    control_rev_per_head = (control["revenue"] / control["n"]) if control["n"] else 0.0
    baseline_revenue = control_rev_per_head * treated["n"]
    incremental = max(0.0, treated["revenue"] - baseline_revenue)

    st_share = 0.25
    return {
        "treated": treated,
        "control": control,
        "uplift_pp": uplift_pp,
        "incremental_returns": max(0, round(treated["n"] * uplift_pp / 100)),
        "gross_revenue": treated["revenue"],
        "baseline_revenue": round(baseline_revenue, 2),
        "incremental_revenue": round(incremental, 2),
        "fee_share": st_share,
        "fee": round(incremental * st_share, 2),
        "windows": {"return_days": cfg.return_window_days, "revenue_days": cfg.revenue_window_days},
    }


def returned_list(conn: sqlite3.Connection, campaign_id: int, limit: int = 200) -> list:
    return db.all_rows(
        conn,
        """SELECT a.*, c.name, c.phone, s.segment, s.reason
           FROM attributions a
           JOIN contacts c ON c.id=a.contact_id
           LEFT JOIN segments s ON s.contact_id=a.contact_id AND s.campaign_id=a.campaign_id
           WHERE a.campaign_id=? AND a.returned_at IS NOT NULL AND a.is_control=0
           ORDER BY a.revenue_60d DESC LIMIT ?""",
        (campaign_id, limit),
    )
