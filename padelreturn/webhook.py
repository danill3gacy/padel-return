"""Приём входящих ответов от провайдера WhatsApp. Стандартная библиотека, без фреймворков.

Запуск: python -m padelreturn.webhook --club "Падел Фили" --campaign 1 --port 8080
Провайдеру (Wazzup/Radist) указывается URL http://ваш-хост:8080/inbound
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import db, inbox
from .config import CONFIG
from .utils import normalize_phone

STATE: dict = {}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/inbound"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        # Нормализуем разные формы вебхуков провайдеров.
        msgs = payload.get("messages") or [payload]
        handled = []
        conn = STATE["conn"]
        for m in msgs:
            phone = normalize_phone(m.get("chatId") or m.get("from") or m.get("phone"))
            text = m.get("text") or m.get("body") or ""
            if not phone or not text:
                continue
            if m.get("fromMe") or m.get("isEcho"):
                continue
            contact = db.one(
                conn, "SELECT id FROM contacts WHERE club_id=? AND phone=?", (STATE["club_id"], phone)
            )
            if not contact:
                handled.append({"phone": phone, "status": "unknown_contact"})
                continue
            res = inbox.handle(conn, STATE["club_id"], STATE["campaign_id"], contact["id"],
                               text, STATE["cfg"], channel="whatsapp")
            handled.append({"phone": phone, "intent": res["intent"], "escalate": res["escalate"]})
            if res["reply"]:
                from . import channels
                c = db.one(conn, "SELECT * FROM contacts WHERE id=?", (contact["id"],))
                channels.pick(STATE["cfg"], c).send(c, res["reply"])
        return self._json(200, {"ok": True, "handled": handled})

    def log_message(self, fmt, *args):
        print(f"[webhook] {fmt % args}")


def run(club: str, db_path: str, campaign_id: int, port: int):
    conn = db.init_db(db_path)
    STATE.update({
        "conn": conn,
        "club_id": db.get_or_create_club(conn, club),
        "campaign_id": campaign_id,
        "cfg": CONFIG,
    })
    print(f"Вебхук слушает :{port}/inbound  (клуб «{club}», кампания {campaign_id})")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--club", default="Демо Падел")
    ap.add_argument("--db", default="padel_return.db")
    ap.add_argument("--campaign", type=int, required=True)
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    run(a.club, a.db, a.campaign, a.port)
