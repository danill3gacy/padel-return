"""Обработка ответов: классификация намерения, диалог, эскалация, задачи администратору.

Правило из PRD: агент ведёт диалог, но действие в реальном мире подтверждает человек.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import db
from . import offers as offers_mod
from .config import Config
from .llm import LLM
from .utils import first_name, iso

INTENTS = ["accept", "reschedule", "price", "question", "negative", "stop", "later", "unclear"]

STOP_WORDS = ["стоп", "stop", "отпишите", "отписаться", "не пишите", "отписка", "unsubscribe",
              "удалите мой номер", "больше не пишите"]
ACCEPT_WORDS = ["да", "давай", "запиши", "записывай", "го", "поехали", "буду", "хорошо",
                "ок", "окей", "согласен", "согласна", "подходит", "можно", "+"]
NEGATIVE_WORDS = ["ужас", "хамств", "верните деньги", "возврат", "жалоб", "роспотребнадзор",
                  "суд", "обман", "отврат", "грязн", "мошенн"]
PRICE_WORDS = ["дорого", "дороговато", "цена", "сколько стоит", "скидк", "дешевле"]
RESCHEDULE_WORDS = ["не могу", "другое время", "другой день", "не подходит время",
                    "занят", "занята", "перенести", "а в"]
LATER_WORDS = ["позже", "потом", "не сейчас", "в отпуске", "уехал", "травм", "болею", "нога", "спина"]

ESCALATE_INTENTS = {"negative"}

SYSTEM = "Ты классификатор ответов клиентов падел-клуба. Отвечаешь только валидным JSON."

PROMPT = """Клиенту отправили сообщение с предложением сыграть. Он ответил.

Наше сообщение: {outbound}
Ответ клиента: {inbound}

Определи намерение. Верни JSON:
{{
  "intent": "<одно из: accept | reschedule | price | question | negative | stop | later | unclear>",
  "escalate": <true, если нужен человек: негатив, жалоба, возврат денег, травма, конфликт>,
  "note": "<одна короткая фраза для администратора>"
}}"""


def classify_rules(text: str) -> dict:
    low = (text or "").lower().strip()
    if any(w in low for w in STOP_WORDS):
        return {"intent": "stop", "escalate": False, "note": "просит отписать"}
    if any(w in low for w in NEGATIVE_WORDS):
        return {"intent": "negative", "escalate": True, "note": "негатив, нужен человек"}
    if any(w in low for w in PRICE_WORDS):
        return {"intent": "price", "escalate": False, "note": "вопрос по цене"}
    if any(w in low for w in LATER_WORDS):
        return {"intent": "later", "escalate": False, "note": "пауза по личным причинам"}
    if any(w in low for w in RESCHEDULE_WORDS):
        return {"intent": "reschedule", "escalate": False, "note": "не подходит время"}
    if re.match(r"^\s*(\+|да\b|ок\b|окей\b|го\b)", low) or any(
        re.search(rf"\b{re.escape(w)}\b", low) for w in ACCEPT_WORDS
    ):
        return {"intent": "accept", "escalate": False, "note": "согласие"}
    if "?" in low:
        return {"intent": "question", "escalate": False, "note": "вопрос"}
    return {"intent": "unclear", "escalate": False, "note": "непонятный ответ"}


def classify(text: str, outbound: str, cfg: Config) -> dict:
    llm = LLM(cfg)
    if llm.enabled:
        res = llm.json(PROMPT.format(outbound=outbound[:400], inbound=text[:400]),
                       system=SYSTEM, max_tokens=200)
        if res and res.get("intent") in INTENTS:
            res["escalate"] = bool(res.get("escalate")) or res["intent"] in ESCALATE_INTENTS
            return res
    return classify_rules(text)


def _reply_text(intent: str, contact, offer: dict | None, cfg: Config, conn, club_id: int) -> str | None:
    name = first_name(contact["name"])
    if intent == "accept":
        when = offer["when_ru"] if offer else "на выбранное время"
        return (f"{name}, отлично. Записываю вас {when}, корт {offer['court_id'] if offer else ''}. "
                "Администратор подтвердит бронь в ближайшее время.")
    if intent == "reschedule":
        alts = _alternatives(conn, club_id, cfg, exclude=offer)
        if alts:
            opts = "; ".join(a["when_ru"] for a in alts[:3])
            return f"{name}, есть ещё такие варианты: {opts}. Какой подойдёт?"
        return f"{name}, а какое время вам было бы удобно? Подберу под него."
    if intent == "price":
        st = db.club_settings(conn, club_id)
        off = int(st.get("price_offpeak", 2800))
        return (f"{name}, днём в будни корт стоит {off} ₽ в час на четверых — это {off // 4} ₽ с человека. "
                "Подобрать вам дневное время?")
    if intent == "later":
        return (f"{name}, понял, не буду сейчас беспокоить. Напишите, когда будете готовы — "
                "подберу игру под ваш уровень.")
    if intent == "stop":
        return "Убрал вас из рассылки, больше писать не буду. Хорошего дня."
    return None


def _alternatives(conn, club_id: int, cfg: Config, exclude: dict | None) -> list[dict]:
    slots = offers_mod.free_slots(conn, club_id, cfg, horizon_days=7)
    out = []
    for s in slots:
        if exclude and s["datetime"] == exclude.get("datetime"):
            continue
        from .utils import fmt_slot_ru
        out.append({"when_ru": fmt_slot_ru(s["datetime"]), "datetime": s["datetime"]})
        if len(out) >= 6:
            break
    return out[::2]


def handle(conn, club_id: int, campaign_id: int, contact_id: int, text: str,
           cfg: Config, channel: str = "whatsapp", as_of: datetime | None = None) -> dict:
    """Обрабатывает один входящий ответ. Возвращает решение и, если нужно, ответ бота."""
    as_of = as_of or datetime.now()
    contact = db.must(conn, "SELECT * FROM contacts WHERE id=?", (contact_id,))
    last_out = db.one(
        conn,
        """SELECT * FROM messages WHERE campaign_id=? AND contact_id=? AND direction='out'
           ORDER BY touch_no DESC LIMIT 1""",
        (campaign_id, contact_id),
    )
    outbound_body = last_out["body"] if last_out else ""
    result = classify(text, outbound_body, cfg)
    intent = result["intent"]

    conn.execute(
        "INSERT INTO inbound (contact_id, campaign_id, channel, body, intent, received_at) VALUES (?,?,?,?,?,?)",
        (contact_id, campaign_id, channel, text, intent, iso(as_of)),
    )
    conn.execute(
        "UPDATE messages SET replied_at=COALESCE(replied_at,?) WHERE campaign_id=? AND contact_id=? AND direction='out'",
        (iso(as_of), campaign_id, contact_id),
    )
    # Ответил — автоматическая последовательность останавливается немедленно.
    conn.execute(
        "UPDATE messages SET status='cancelled' WHERE campaign_id=? AND contact_id=? "
        "AND direction='out' AND status IN ('queued','pending_approval')",
        (campaign_id, contact_id),
    )

    offer = None
    if last_out and last_out["offer_id"]:
        offer = offers_mod._offer_dict(conn, last_out["offer_id"])

    if intent == "stop":
        conn.execute(
            "UPDATE contacts SET stop_list=1, stop_reason='запрос клиента' WHERE id=?", (contact_id,)
        )
    if intent == "accept" and offer:
        offer = offers_mod.accept(conn, offer["id"], contact_id)
        conn.execute(
            "INSERT INTO tasks (club_id, contact_id, campaign_id, kind, payload_json) VALUES (?,?,?,?,?)",
            (club_id, contact_id, campaign_id, "confirm_booking",
             json.dumps({
                 "offer_id": offer["id"],
                 "when": offer["when_ru"],
                 "court": offer["court_id"],
                 "price": offer["price"],
                 "client": contact["name"],
                 "phone": contact["phone"],
                 "seats": f"{offer['seats_filled']}/{offer['seats_total']}",
             }, ensure_ascii=False)),
        )
    if result.get("escalate"):
        conn.execute(
            "UPDATE conversations SET escalated=1, escalation_note=?, state='escalated' "
            "WHERE contact_id=? AND campaign_id=?",
            (result.get("note", "")[:200], contact_id, campaign_id),
        )
        conn.execute(
            "INSERT INTO tasks (club_id, contact_id, campaign_id, kind, payload_json) VALUES (?,?,?,?,?)",
            (club_id, contact_id, campaign_id, "escalation",
             json.dumps({"note": result.get("note"), "text": text, "client": contact["name"],
                         "phone": contact["phone"]}, ensure_ascii=False)),
        )

    reply = None if result.get("escalate") else _reply_text(intent, contact, offer, cfg, conn, club_id)
    if reply:
        conn.execute(
            """INSERT INTO messages (campaign_id, contact_id, offer_id, touch_no, direction,
                   channel, template_key, body, status, sent_at)
               VALUES (?,?,?,?, 'out_reply', ?, 'agent_reply', ?, 'sent', ?)""",
            (campaign_id, contact_id, offer["id"] if offer else None,
             90 + db.scalar(conn, "SELECT COUNT(*) FROM messages WHERE campaign_id=? AND contact_id=? AND direction='out_reply'",
                            (campaign_id, contact_id)),
             channel, reply, iso(as_of)),
        )
    conn.execute(
        "UPDATE conversations SET last_msg_at=?, state=? WHERE contact_id=? AND campaign_id=?",
        (iso(as_of), "escalated" if result.get("escalate") else intent, contact_id, campaign_id),
    )
    conn.commit()
    return {"intent": intent, "escalate": bool(result.get("escalate")), "reply": reply,
            "note": result.get("note"), "offer": offer}


def pending_tasks(conn, club_id: int, kind: str | None = None) -> list:
    sql = """SELECT t.*, c.name, c.phone FROM tasks t
             LEFT JOIN contacts c ON c.id=t.contact_id
             WHERE t.club_id=? AND t.state='pending'"""
    params: list[object] = [club_id]
    if kind:
        sql += " AND t.kind=?"
        params.append(kind)
    return db.all_rows(conn, sql + " ORDER BY t.id", params)


def resolve_task(conn, task_id: int, actor: str = "admin") -> None:
    conn.execute(
        "UPDATE tasks SET state='done', resolved_at=datetime('now'), resolved_by=? WHERE id=?",
        (actor, task_id),
    )
    conn.commit()
