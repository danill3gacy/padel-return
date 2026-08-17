"""Админ-бот в Telegram. Это вся админка продукта — веб-интерфейса нет намеренно.

Запуск: PADEL_TG_TOKEN=... python -m padelreturn.bot --club "Падел Фили"
Работает на long-polling, без вебхуков и без зависимостей.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import attribution, db, inbox
from . import campaign as camp_mod
from .config import CONFIG


class TG:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **params: Any) -> Any:
        url = f"{self.base}/{method}"
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=65) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"[tg] {method}: {e}")
            return {"ok": False}

    def send(self, chat_id: int | str, text: str, keyboard: list | None = None) -> Any:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)


HELP = (
    "<b>Возврат клиентов</b>\n\n"
    "/tasks — очередь подтверждений\n"
    "/stats — воронка текущей кампании\n"
    "/money — сколько вернули и сколько к оплате\n"
    "/approve — подтвердить отправку всех подготовленных сообщений\n"
    "/escalations — что требует человека"
)


def fmt_task(t: sqlite3.Row) -> tuple[str, list]:
    p = json.loads(t["payload_json"] or "{}")
    if t["kind"] == "confirm_booking":
        text = (
            f"<b>Подтвердить бронь</b>\n"
            f"{t['name']} · {t['phone']}\n"
            f"{p.get('when')} · корт {p.get('court')} · {int(p.get('price', 0))} ₽\n"
            f"мест в игре: {p.get('seats')}"
        )
    else:
        text = (
            f"<b>Нужен человек</b>\n{t['name']} · {t['phone']}\n"
            f"{p.get('note')}\n\n«{p.get('text', '')}»"
        )
    kb = [
        [
            {"text": "Подтвердить", "callback_data": f"done:{t['id']}"},
            {"text": "Отклонить", "callback_data": f"skip:{t['id']}"},
        ]
    ]
    return text, kb


def run(club: str, db_path: str, campaign_id: int | None) -> None:
    cfg = CONFIG
    if not cfg.tg_bot_token:
        raise SystemExit("Нет PADEL_TG_TOKEN")
    tg = TG(cfg.tg_bot_token)
    conn = db.init_db(db_path)
    club_id = db.get_or_create_club(conn, club)
    offset = 0
    print(f"Админ-бот запущен для клуба «{club}».")

    def current_campaign() -> int | None:
        if campaign_id:
            return campaign_id
        row = db.one(
            conn, "SELECT id FROM campaigns WHERE club_id=? ORDER BY id DESC LIMIT 1", (club_id,)
        )
        return row["id"] if row else None

    while True:
        upd = tg.call("getUpdates", offset=offset, timeout=50)
        for u in upd.get("result", []):
            offset = u["update_id"] + 1

            if "callback_query" in u:
                cq = u["callback_query"]
                action, _, tid = cq["data"].partition(":")
                inbox.resolve_task(conn, int(tid), actor=str(cq["from"].get("username", "admin")))
                tg.call(
                    "answerCallbackQuery",
                    callback_query_id=cq["id"],
                    text="Готово" if action == "done" else "Отклонено",
                )
                tg.call(
                    "editMessageText",
                    chat_id=cq["message"]["chat"]["id"],
                    message_id=cq["message"]["message_id"],
                    text=cq["message"]["text"]
                    + ("\n\n✓ подтверждено" if action == "done" else "\n\n× отклонено"),
                )
                continue

            msg = u.get("message") or {}
            chat = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()
            if not chat:
                continue
            cid = current_campaign()

            if text in ("/start", "/help"):
                tg.send(chat, HELP)
            elif text == "/tasks":
                tasks = inbox.pending_tasks(conn, club_id, "confirm_booking")
                if not tasks:
                    tg.send(chat, "Очередь пуста.")
                for t in tasks[:15]:
                    body, kb = fmt_task(t)
                    tg.send(chat, body, kb)
                if len(tasks) > 15:
                    tg.send(chat, f"…и ещё {len(tasks) - 15}.")
            elif text == "/escalations":
                tasks = inbox.pending_tasks(conn, club_id, "escalation")
                if not tasks:
                    tg.send(chat, "Эскалаций нет.")
                for t in tasks[:10]:
                    body, kb = fmt_task(t)
                    tg.send(chat, body, kb)
            elif text == "/stats" and cid:
                s = camp_mod.stats(conn, cid)
                tg.send(
                    chat,
                    (
                        f"<b>Кампания #{cid}</b>\n"
                        f"аудитория: {s['audience']} (контроль {s['control']})\n"
                        f"отправлено: {s['messages_sent']}\n"
                        f"ответили: {s['replied']} ({s['reply_rate']}%)\n"
                        f"согласились: {s['accepted']} ({s['accept_rate']}%)\n"
                        f"отписались: {s['stopped']} ({s['stop_rate']}%)\n"
                        f"расходы: {s['cost']:.0f} ₽"
                    ),
                )
            elif text == "/money" and cid:
                a = attribution.report(conn, cid, cfg)
                tg.send(
                    chat,
                    (
                        f"<b>Результат кампании #{cid}</b>\n"
                        f"вернулось: {a['treated']['returned']} из {a['treated']['n']} "
                        f"({a['treated']['rate']}%)\n"
                        f"контроль: {a['control']['rate']}%\n"
                        f"прирост: +{a['uplift_pp']} п.п.\n"
                        f"инкрементальная выручка: {a['incremental_revenue']:.0f} ₽\n"
                        f"к оплате: {a['fee']:.0f} ₽"
                    ),
                )
            elif text == "/approve" and cid:
                n = camp_mod.approve(conn, cid)
                tg.send(chat, f"Подтверждено сообщений: {n}")
            else:
                tg.send(chat, HELP)
        time.sleep(0.4)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--club", default="Демо Падел")
    ap.add_argument("--db", default="padel_return.db")
    ap.add_argument("--campaign", type=int)
    a = ap.parse_args()
    run(a.club, a.db, a.campaign)
