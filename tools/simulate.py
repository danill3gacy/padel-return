"""Симулятор кампании: играет за клиентов, чтобы можно было прогнать продукт целиком.

Нужен только для демо и для отладки. В бою ответы приходят вебхуком провайдера.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from padelreturn import db, inbox, attribution, campaign as camp_mod, importer, report  # noqa: E402
from padelreturn.config import CONFIG  # noqa: E402
from tools.gen_sample_data import simulate_future  # noqa: E402

# Как отвечают живые люди. Вероятности подобраны под бенчмарки из PRD:
# ответ 15-25%, согласие 8-12%, отписка <3%.
REPLIES = {
    "accept": ["Да, записывайте", "Давайте", "Го", "Ок, буду", "+", "Да, подходит",
               "Хорошо, запишите меня", "Да, давно хотел вернуться"],
    "reschedule": ["Не могу в это время, есть что-то вечером?", "В это время работаю",
                   "А в выходные есть?", "Другой день можно?"],
    "price": ["А сколько стоит?", "Дороговато стало", "Скидки есть?"],
    "later": ["Сейчас в отпуске, попозже", "Травма, пока не играю", "Не сейчас, спасибо"],
    "question": ["Ракетку можно взять в аренду?", "А парковка есть?"],
    "negative": ["В прошлый раз в раздевалке была грязь, больше не приду"],
    "stop": ["Стоп", "Отпишите меня"],
}
# Доли ответивших по типам
MIX = (["accept"] * 42 + ["reschedule"] * 20 + ["price"] * 10 + ["later"] * 12 +
       ["question"] * 8 + ["negative"] * 3 + ["stop"] * 5)


def run(db_path: str, club: str, campaign_id: int, reply_rate: float, seed: int,
        bookings_src: str, out_report: str):
    rnd = random.Random(seed)
    conn = db.init_db(db_path)
    cfg = CONFIG.merged({"default_channel": "console", "require_approval": False})
    club_id = db.get_or_create_club(conn, club)
    camp = camp_mod.get(conn, campaign_id)
    from padelreturn.utils import parse_dt
    as_of = parse_dt(camp["started_at"]) or datetime.now()

    sent = db.all_rows(
        conn,
        """SELECT m.contact_id, c.external_id FROM messages m JOIN contacts c ON c.id=m.contact_id
           WHERE m.campaign_id=? AND m.touch_no=1 AND m.status='sent'""",
        (campaign_id,),
    )
    print(f"Отправлено сообщений: {len(sent)}")

    answered = 0
    accepted_ext: list[str] = []
    for row in sent:
        if rnd.random() > reply_rate:
            continue
        intent = rnd.choice(MIX)
        text = rnd.choice(REPLIES[intent])
        res = inbox.handle(conn, club_id, campaign_id, row["contact_id"], text, cfg,
                           channel="whatsapp", as_of=as_of + timedelta(hours=rnd.randint(1, 40)))
        answered += 1
        if res["intent"] == "accept":
            accepted_ext.append(row["external_id"])
        # часть тех, кто просил перенос, соглашается на альтернативу
        elif res["intent"] == "reschedule" and rnd.random() < 0.45:
            inbox.handle(conn, club_id, campaign_id, row["contact_id"], "Да, давайте так", cfg,
                         channel="whatsapp", as_of=as_of + timedelta(hours=rnd.randint(41, 60)))
            accepted_ext.append(row["external_id"])

    print(f"Ответили: {answered}   Согласились: {len(accepted_ext)}")

    # Часть согласившихся реально доходит до корта (плюс небольшой органический возврат).
    came = [e for e in accepted_ext if rnd.random() < 0.72]
    all_ext = {r["external_id"] for r in db.all_rows(
        conn, "SELECT c.external_id FROM segments s JOIN contacts c ON c.id=s.contact_id "
              "WHERE s.campaign_id=? AND s.sleeping=1", (campaign_id,))}
    organic = [e for e in all_ext - set(came) if rnd.random() < 0.055]
    print(f"Дошли до корта: {len(came)}   Органический возврат (в т.ч. контроль): {len(organic)}")

    future = simulate_future(bookings_src, "data/sample/bookings_after.csv",
                             came + organic, as_of, seed=seed)
    importer.import_bookings(conn, club_id, future)

    later = as_of + timedelta(days=61)
    res = attribution.compute(conn, club_id, campaign_id, cfg, as_of=later)

    print("\n=== АТРИБУЦИЯ ===")
    t, c = res["treated"], res["control"]
    print(f"Основная группа : {t['returned']}/{t['n']} = {t['rate']}%   выручка {t['revenue']:,.0f} ₽")
    print(f"Контроль        : {c['returned']}/{c['n']} = {c['rate']}%")
    print(f"Прирост         : +{res['uplift_pp']} п.п.")
    print(f"Инкрементальная выручка: {res['incremental_revenue']:,.0f} ₽")
    print(f"К оплате (25%)  : {res['fee']:,.0f} ₽")

    print("\n=== ЗАДАЧИ АДМИНИСТРАТОРУ ===")
    tasks = inbox.pending_tasks(conn, club_id)
    kinds = {}
    for t_ in tasks:
        kinds[t_["kind"]] = kinds.get(t_["kind"], 0) + 1
    print(kinds or "нет")

    path = report.build(conn, club_id, campaign_id, cfg, out_report)
    print(f"\nОтчёт: {path}")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="padel_return.db")
    ap.add_argument("--club", default="Падел Фили")
    ap.add_argument("--campaign", type=int, default=1)
    ap.add_argument("--reply-rate", type=float, default=0.19)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--bookings", default="data/sample/bookings.csv")
    ap.add_argument("--out", default="report.html")
    a = ap.parse_args()
    run(a.db, a.club, a.campaign, a.reply_rate, a.seed, a.bookings, a.out)
