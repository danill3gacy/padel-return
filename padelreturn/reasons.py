"""Гипотеза причины ухода: закрытый список причин, LLM опционально, правила — всегда.

Правило из PRD: при confidence < 0.5 причина принудительно становится 'неизвестно'.
Уверенная неправда хуже честной нейтральности.
"""

from __future__ import annotations

import sqlite3

from . import db
from .config import Config
from .features import top_partners
from .llm import LLM
from .utils import RU_DOW, parse_dt

REASONS = [
    "нет_партнёров",
    "сменился_график",
    "ушёл_тренер",
    "слишком_дорого",
    "не_понравился_первый_опыт",
    "уровень_не_совпал",
    "травма_пауза",
    "сезонность",
    "ушёл_в_другой_клуб",
    "неизвестно",
]

OFFERS = [
    "собранная_игра",
    "новичковая_сессия",
    "слот_в_привычное_время",
    "другой_тренер",
    "офф-пик_цена",
    "турнир_американо",
]

MIN_CONFIDENCE = 0.5

SYSTEM = "Ты аналитик падел-клуба. Отвечаешь только валидным JSON, без пояснений."

PROMPT = """По истории клиента определи наиболее вероятную причину, по которой он перестал ходить.

Данные клиента:
{facts}

Верни JSON строго такого вида:
{{
  "reason": "<одно значение из: {reasons}>",
  "confidence": <число 0.0-1.0>,
  "evidence": "<одна короткая фраза: какой факт в данных на это указывает>",
  "best_offer": "<одно из: {offers}>"
}}

Не выдумывай фактов, которых нет в данных. Если данных мало — ставь confidence ниже 0.5."""


def build_facts(conn: sqlite3.Connection, club_id: int, row: sqlite3.Row, cfg: Config) -> str:
    partners = top_partners(conn, club_id, row["contact_id"], limit=3)
    p_lines = []
    for p in partners:
        gone = ""
        if p["days_since_last"] is not None:
            thr = max(cfg.sleeping_min_days, (p["avg_interval"] or 0) * cfg.sleeping_interval_mult)
            gone = (
                " — тоже перестал ходить" if p["days_since_last"] > thr else " — продолжает ходить"
            )
        p_lines.append(f"{p['name']} ({p['games_count']} совместных игр{gone})")

    coach_status = ""
    if row["main_coach"]:
        active = db.scalar(
            conn,
            """SELECT COUNT(*) FROM bookings WHERE club_id=? AND coach_id=? AND status='done'
               AND julianday('now') - julianday(starts_at) < 45""",
            (club_id, row["main_coach"]),
        )
        coach_status = (
            f"{row['main_coach']} ({'работает' if active else 'больше не ведёт занятия'})"
        )

    dow = RU_DOW.get(row["usual_dow"], "—") if row["usual_dow"] is not None else "—"
    hour = f"{row['usual_hour']}:00" if row["usual_hour"] is not None else "—"
    last = parse_dt(row["last_visit"])
    avg_check = (row["revenue_total"] or 0) / row["visits_total"] if row["visits_total"] else 0

    return "\n".join(
        [
            f"Имя: {row['name']}",
            f"Всего визитов: {row['visits_total']}",
            f"Последний визит: {last.strftime('%d.%m.%Y') if last else '—'} "
            f"({row['days_since_last']} дней назад)",
            f"Обычное время игры: {dow}, {hour}",
            f"Средний интервал между визитами: "
            f"{round(row['avg_interval']) if row['avg_interval'] else '—'} дней",
            f"Доля тренировок: {round((row['lessons_share'] or 0) * 100)}%",
            f"Турниров сыграно: {row['tournaments'] or 0}",
            f"Постоянные партнёры: {'; '.join(p_lines) if p_lines else 'нет'}",
            f"Тренер: {coach_status or 'не занимался'}",
            f"Средний чек: {round(avg_check)} ₽",
            f"Доля неявок: {round((row['no_show_rate'] or 0) * 100)}%",
        ]
    )


def infer_rules(conn: sqlite3.Connection, club_id: int, row: sqlite3.Row, cfg: Config) -> dict:
    """Детерминированный фолбэк. Работает без LLM и служит sanity-check для неё."""
    visits = row["visits_total"] or 0

    partners = top_partners(conn, club_id, row["contact_id"], limit=3)
    for p in partners:
        if p["games_count"] >= 3 and p["days_since_last"] is not None:
            thr = max(cfg.sleeping_min_days, (p["avg_interval"] or 0) * cfg.sleeping_interval_mult)
            if p["days_since_last"] > thr:
                return {
                    "reason": "нет_партнёров",
                    "confidence": 0.72,
                    "evidence": (
                        f"постоянный партнёр {p['name']} "
                        f"({p['games_count']} игр) тоже перестал ходить"
                    ),
                    "best_offer": "собранная_игра",
                }

    if row["main_coach"]:
        active = db.scalar(
            conn,
            """SELECT COUNT(*) FROM bookings WHERE club_id=? AND coach_id=? AND status='done'
               AND julianday('now') - julianday(starts_at) < 45""",
            (club_id, row["main_coach"]),
        )
        if not active and (row["lessons_share"] or 0) > 0.5:
            return {
                "reason": "ушёл_тренер",
                "confidence": 0.68,
                "evidence": (
                    f"занимался в основном с тренером {row['main_coach']}, "
                    f"который больше не ведёт занятия"
                ),
                "best_offer": "другой_тренер",
            }

    if visits == 1:
        return {
            "reason": "не_понравился_первый_опыт",
            "confidence": 0.55,
            "evidence": "был всего один раз и не вернулся",
            "best_offer": "новичковая_сессия",
        }
    if visits <= 3:
        return {
            "reason": "нет_партнёров",
            "confidence": 0.58,
            "evidence": f"всего {visits} визита, постоянных партнёров не появилось",
            "best_offer": "турнир_американо",
        }

    if row["usual_dow"] is not None and row["usual_hour"] is not None:
        slot_alive = db.scalar(
            conn,
            """SELECT COUNT(*) FROM bookings WHERE club_id=? AND status='done'
               AND CAST(strftime('%w', starts_at) AS INTEGER) = ?
               AND CAST(strftime('%H', starts_at) AS INTEGER) = ?
               AND julianday('now') - julianday(starts_at) < 45""",
            (club_id, (row["usual_dow"] + 1) % 7, row["usual_hour"]),
        )
        if not slot_alive:
            return {
                "reason": "сменился_график",
                "confidence": 0.6,
                "evidence": f"играл по {RU_DOW[row['usual_dow']]} в {row['usual_hour']}:00, "
                "в это время игр больше нет",
                "best_offer": "слот_в_привычное_время",
            }

    if visits >= cfg.loyal_min_visits:
        return {
            "reason": "неизвестно",
            "confidence": 0.4,
            "evidence": f"постоянный клиент с {visits} визитами, явной причины в данных нет",
            "best_offer": "слот_в_привычное_время",
        }

    return {
        "reason": "неизвестно",
        "confidence": 0.35,
        "evidence": "данных недостаточно",
        "best_offer": "собранная_игра",
    }


def infer(
    conn: sqlite3.Connection, club_id: int, campaign_id: int, cfg: Config, use_llm: bool = True
) -> dict:
    from .segmentation import audience

    llm = LLM(cfg)
    rows = audience(conn, campaign_id, include_control=False)
    stats = {"total": len(rows), "llm": 0, "rules": 0, "forced_unknown": 0}
    by_reason: dict[str, int] = {}

    for row in rows:
        result = None
        if use_llm and llm.enabled:
            facts = build_facts(conn, club_id, row, cfg)
            prompt = PROMPT.format(
                facts=facts, reasons=" | ".join(REASONS), offers=" | ".join(OFFERS)
            )
            result = llm.json(prompt, system=SYSTEM, max_tokens=400)
            if result and result.get("reason") in REASONS:
                stats["llm"] += 1
            else:
                result = None

        if result is None:
            result = infer_rules(conn, club_id, row, cfg)
            stats["rules"] += 1

        conf = float(result.get("confidence") or 0)
        reason = result.get("reason", "неизвестно")
        if conf < MIN_CONFIDENCE:
            reason = "неизвестно"
            stats["forced_unknown"] += 1

        offer = result.get("best_offer")
        if offer not in OFFERS:
            offer = "собранная_игра"

        by_reason[reason] = by_reason.get(reason, 0) + 1
        conn.execute(
            """UPDATE segments SET reason=?, confidence=?, evidence=?, best_offer=?
               WHERE campaign_id=? AND contact_id=?""",
            (reason, conf, result.get("evidence", "")[:400], offer, campaign_id, row["contact_id"]),
        )

    conn.commit()
    payload: dict[str, object] = {**stats, "by_reason": by_reason}
    db.log(conn, "reasons", "infer", payload)
    conn.commit()
    return payload
