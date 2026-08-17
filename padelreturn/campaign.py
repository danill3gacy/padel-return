"""Оркестрация кампании: планирование касаний, волны, каденция, отправка с подтверждением."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import channels, db
from . import offers as offers_mod
from .config import Config
from .llm import LLM
from .messages import generate, violates, with_footer
from .segmentation import audience
from .utils import iso


def create(conn, club_id: int, name: str, cfg: Config, config_overrides: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO campaigns (club_id, name, status, config_json) VALUES (?,?,?,?)",
        (club_id, name, "draft", json.dumps(config_overrides or {}, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid


def get(conn, campaign_id: int):
    return db.one(conn, "SELECT * FROM campaigns WHERE id=?", (campaign_id,))


def plan(conn, club_id: int, campaign_id: int, cfg: Config, as_of: datetime | None = None,
         limit: int | None = None) -> dict:
    """Готовит первое касание для всей аудитории: оффер + текст + время отправки.

    Ничего не отправляет. Всё, что здесь создано, можно вычитать глазами.
    """
    as_of = as_of or datetime.now()
    club = db.must(conn, "SELECT * FROM clubs WHERE id=?", (club_id,))
    llm = LLM(cfg)
    rows = audience(conn, campaign_id, include_control=False)
    if limit:
        rows = rows[:limit]

    slots = offers_mod.free_slots(conn, club_id, cfg, as_of=as_of)
    stats = {"planned": 0, "skipped": 0, "llm": 0, "template": 0, "waves": 0}

    wave, day_offset = 0, 0
    for i, row in enumerate(rows):
        if i and i % cfg.wave_size == 0:
            day_offset += 1
            wave += 1
        exists = db.one(
            conn,
            "SELECT id FROM messages WHERE campaign_id=? AND contact_id=? AND touch_no=1 AND direction='out'",
            (campaign_id, row["contact_id"]),
        )
        if exists:
            stats["skipped"] += 1
            continue

        offer = offers_mod.plan_offer(conn, club_id, campaign_id, row, cfg, slots, as_of=as_of)
        text, source = generate(row, offer, club["name"], cfg, touch=1, llm=llm)
        body = with_footer(text, 1)
        stats[source if source in ("llm", "template") else "template"] += 1

        contact = db.one(conn, "SELECT * FROM contacts WHERE id=?", (row["contact_id"],))
        ch = channels.pick(cfg, contact)
        scheduled = _schedule_at(as_of + timedelta(days=day_offset), cfg)

        conn.execute(
            """INSERT INTO messages (campaign_id, contact_id, offer_id, touch_no, direction,
                                     channel, template_key, body, status, scheduled_at)
               VALUES (?,?,?,?,'out',?,?,?,?,?)""",
            (campaign_id, row["contact_id"], offer["id"], 1, ch.name,
             f"return_touch1_{offer['kind']}", body,
             "pending_approval" if cfg.require_approval else "queued", iso(scheduled)),
        )
        stats["planned"] += 1

    stats["waves"] = wave + 1
    conn.execute("UPDATE campaigns SET status='planned' WHERE id=?", (campaign_id,))
    conn.commit()
    db.log(conn, "campaign", "plan", {"campaign_id": campaign_id, **stats})
    conn.commit()
    return stats


def _schedule_at(day: datetime, cfg: Config) -> datetime:
    """Не пишем в тихие часы."""
    start_quiet, end_quiet = cfg.quiet_hours
    h = day.hour
    if h >= start_quiet:
        day = (day + timedelta(days=1)).replace(hour=end_quiet, minute=0, second=0, microsecond=0)
    elif h < end_quiet:
        day = day.replace(hour=end_quiet, minute=0, second=0, microsecond=0)
    return day


def preview(conn, campaign_id: int, n: int = 30) -> list[dict]:
    rows = db.all_rows(
        conn,
        """SELECT m.id, m.body, m.channel, m.status, m.touch_no, m.scheduled_at,
                  c.name, c.phone, s.segment, s.reason, s.confidence, s.evidence,
                  o.kind, o.slot_datetime, o.seats_filled, o.seats_total
           FROM messages m
           JOIN contacts c ON c.id = m.contact_id
           LEFT JOIN segments s ON s.contact_id = m.contact_id AND s.campaign_id = m.campaign_id
           LEFT JOIN offers o ON o.id = m.offer_id
           WHERE m.campaign_id=? AND m.direction='out'
           ORDER BY m.touch_no, m.id LIMIT ?""",
        (campaign_id, n),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["warning"] = violates(r["body"].split("\n\n")[0])
        out.append(d)
    return out


def approve(conn, campaign_id: int, message_ids: list[int] | None = None, actor: str = "admin") -> int:
    if message_ids:
        q = ",".join("?" * len(message_ids))
        cur = conn.execute(
            f"UPDATE messages SET status='queued', approved_by=? "
            f"WHERE campaign_id=? AND status='pending_approval' AND id IN ({q})",
            (actor, campaign_id, *message_ids),
        )
    else:
        cur = conn.execute(
            "UPDATE messages SET status='queued', approved_by=? "
            "WHERE campaign_id=? AND status='pending_approval'",
            (actor, campaign_id),
        )
    conn.commit()
    db.log(conn, actor, "approve", {"campaign_id": campaign_id, "n": cur.rowcount})
    conn.commit()
    return cur.rowcount


def run(conn, campaign_id: int, cfg: Config, as_of: datetime | None = None,
        max_send: int | None = None) -> dict:
    """Отправляет всё, что подтверждено и подошло по времени."""
    as_of = as_of or datetime.now()
    limit = max_send or cfg.wave_size
    rows = db.all_rows(
        conn,
        """SELECT m.*, c.name, c.phone, c.tg_chat_id, c.stop_list
           FROM messages m JOIN contacts c ON c.id = m.contact_id
           WHERE m.campaign_id=? AND m.direction='out' AND m.status='queued'
             AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?)
           ORDER BY m.scheduled_at LIMIT ?""",
        (campaign_id, iso(as_of), limit),
    )
    stats = {"sent": 0, "failed": 0, "skipped_stop": 0, "cost": 0.0}

    for m in rows:
        if m["stop_list"]:
            conn.execute("UPDATE messages SET status='skipped', error='стоп-лист' WHERE id=?", (m["id"],))
            stats["skipped_stop"] += 1
            continue
        contact = db.one(conn, "SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
        ch = channels.pick(cfg, contact)
        res = ch.send(contact, m["body"], m["template_key"])
        if res.ok:
            conn.execute(
                "UPDATE messages SET status='sent', channel=?, sent_at=?, cost=? WHERE id=?",
                (res.channel, iso(as_of), res.cost, m["id"]),
            )
            conn.execute(
                """INSERT OR IGNORE INTO conversations (club_id, contact_id, campaign_id, channel, state, last_msg_at)
                   VALUES ((SELECT club_id FROM campaigns WHERE id=?), ?, ?, ?, 'awaiting_reply', ?)""",
                (campaign_id, m["contact_id"], campaign_id, res.channel, iso(as_of)),
            )
            stats["sent"] += 1
            stats["cost"] += res.cost
        else:
            conn.execute(
                "UPDATE messages SET status='failed', error=? WHERE id=?", (res.error, m["id"])
            )
            stats["failed"] += 1

    conn.execute("UPDATE campaigns SET status='running', started_at=COALESCE(started_at,?) WHERE id=?",
                 (iso(as_of), campaign_id))
    conn.commit()
    db.log(conn, "campaign", "run", {"campaign_id": campaign_id, **stats})
    conn.commit()
    return stats


def schedule_followups(conn, club_id: int, campaign_id: int, cfg: Config,
                       as_of: datetime | None = None) -> dict:
    """Касания 2 и 3 — только тем, кто не ответил. Ответил что угодно — автоматика молчит."""
    as_of = as_of or datetime.now()
    club = db.must(conn, "SELECT * FROM clubs WHERE id=?", (club_id,))
    llm = LLM(cfg)
    slots = offers_mod.free_slots(conn, club_id, cfg, as_of=as_of)
    stats = {"touch2": 0, "touch3": 0}

    for touch, delay in ((2, cfg.touch_2_delay_days), (3, cfg.touch_3_delay_days)):
        if touch > cfg.max_touches:
            break
        candidates = db.all_rows(
            conn,
            """SELECT m.contact_id, m.sent_at
               FROM messages m
               WHERE m.campaign_id=? AND m.touch_no=1 AND m.direction='out' AND m.status='sent'
                 AND julianday(?) - julianday(m.sent_at) >= ?
                 AND NOT EXISTS (SELECT 1 FROM inbound i
                                 WHERE i.contact_id=m.contact_id AND i.campaign_id=m.campaign_id)
                 AND NOT EXISTS (SELECT 1 FROM messages m2
                                 WHERE m2.campaign_id=m.campaign_id AND m2.contact_id=m.contact_id
                                   AND m2.touch_no=? AND m2.direction='out')
                 AND (SELECT stop_list FROM contacts WHERE id=m.contact_id)=0""",
            (campaign_id, iso(as_of), delay, touch),
        )
        for cand in candidates:
            row = db.one(
                conn,
                """SELECT s.contact_id, s.segment, s.reason, s.best_offer, c.name,
                          f.visits_total, f.usual_dow, f.usual_hour, f.last_visit, f.level_proxy
                   FROM segments s JOIN contacts c ON c.id=s.contact_id
                   LEFT JOIN features f ON f.contact_id=s.contact_id
                   WHERE s.campaign_id=? AND s.contact_id=?""",
                (campaign_id, cand["contact_id"]),
            )
            if not row:
                continue
            if touch == 2:
                offer = offers_mod.plan_offer(conn, club_id, campaign_id, row, cfg, slots, as_of=as_of)
            else:
                offer = {"id": None, "kind": "closing", "kind_title": "закрытие", "when_ru": "",
                         "seats_left": 0, "seats_filled": 0, "price": 0,
                         "datetime": as_of, "court_id": "", "seats_total": 0,
                         "level_min": 0, "level_max": 0}
            text, _ = generate(row, offer, club["name"], cfg, touch=touch, llm=llm)
            conn.execute(
                """INSERT OR IGNORE INTO messages (campaign_id, contact_id, offer_id, touch_no,
                       direction, channel, template_key, body, status, scheduled_at)
                   VALUES (?,?,?,?, 'out', ?, ?, ?, ?, ?)""",
                (campaign_id, cand["contact_id"], offer["id"], touch, cfg.default_channel,
                 f"return_touch{touch}", with_footer(text, touch),
                 "pending_approval" if cfg.require_approval else "queued", iso(as_of)),
            )
            stats[f"touch{touch}"] += 1

    conn.commit()
    db.log(conn, "campaign", "followups", {"campaign_id": campaign_id, **stats})
    conn.commit()
    return stats


def stats(conn, campaign_id: int) -> dict:
    def s(sql, params=(), default=0):
        return db.scalar(conn, sql, params, default)

    sent = s("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND direction='out' AND status='sent'", (campaign_id,))
    planned = s("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND direction='out'", (campaign_id,))
    replied = s("SELECT COUNT(DISTINCT contact_id) FROM inbound WHERE campaign_id=?", (campaign_id,))
    accepted = s(
        """SELECT COUNT(DISTINCT os.contact_id) FROM offer_seats os
           JOIN offers o ON o.id=os.offer_id WHERE o.campaign_id=? AND os.state='accepted'""",
        (campaign_id,),
    )
    stopped = s("SELECT COUNT(*) FROM inbound WHERE campaign_id=? AND intent='stop'", (campaign_id,))
    negative = s("SELECT COUNT(*) FROM inbound WHERE campaign_id=? AND intent='negative'", (campaign_id,))
    cost = s("SELECT COALESCE(SUM(cost),0) FROM messages WHERE campaign_id=?", (campaign_id,), 0.0)
    audience_n = s("SELECT COUNT(*) FROM segments WHERE campaign_id=? AND sleeping=1 AND is_control=0 AND segment IS NOT NULL AND segment<>'E'", (campaign_id,))
    control_n = s("SELECT COUNT(*) FROM segments WHERE campaign_id=? AND sleeping=1 AND is_control=1", (campaign_id,))

    return {
        "audience": audience_n,
        "control": control_n,
        "messages_planned": planned,
        "messages_sent": sent,
        "replied": replied,
        "reply_rate": round(replied / sent * 100, 1) if sent else 0.0,
        "accepted": accepted,
        "accept_rate": round(accepted / sent * 100, 1) if sent else 0.0,
        "stopped": stopped,
        "stop_rate": round(stopped / sent * 100, 1) if sent else 0.0,
        "negative": negative,
        "cost": round(cost, 2),
        "cost_per_contact": round(cost / sent, 2) if sent else 0.0,
    }
