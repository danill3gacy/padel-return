"""Генератор правдоподобной выгрузки падел-клуба — чтобы продукт запускался из коробки.

Моделирует реальную структуру базы: ядро постоянных, отвалившихся новичков,
пары постоянных партнёров, ушедшего тренера, сезонных, no-show-проблемных.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import datetime, timedelta

FIRST_M = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Алексей", "Артём", "Илья",
           "Кирилл", "Михаил", "Никита", "Матвей", "Роман", "Егор", "Арсений", "Иван",
           "Денис", "Евгений", "Тимур", "Владислав", "Павел", "Антон", "Глеб", "Марк"]
FIRST_F = ["Анна", "Мария", "Елена", "Дарья", "Алина", "Ирина", "Екатерина", "Ольга",
           "Полина", "Юлия", "Ксения", "Виктория", "София", "Наталья", "Татьяна", "Вера",
           "Александра", "Милана", "Кристина", "Валерия"]
LAST = ["Иванов", "Смирнов", "Кузнецов", "Попов", "Соколов", "Лебедев", "Козлов", "Новиков",
        "Морозов", "Петров", "Волков", "Соловьёв", "Васильев", "Зайцев", "Павлов", "Семёнов",
        "Голубев", "Виноградов", "Богданов", "Воробьёв", "Фёдоров", "Михайлов", "Беляев", "Тарасов"]

COACHES = ["Хорхе", "Марат", "Алина Т.", "Диего"]
GONE_COACH = "Диего"          # уволился 3 месяца назад
COURTS = ["C1", "C2", "C3", "C4"]

TYPES = ["rent", "rent", "rent", "rent", "lesson", "group", "tournament"]


def name_for(gender: str, rnd: random.Random) -> str:
    first = rnd.choice(FIRST_F if gender == "f" else FIRST_M)
    last = rnd.choice(LAST) + ("а" if gender == "f" else "")
    return f"{first} {last}"


def price_for(dt: datetime, btype: str) -> float:
    if btype == "lesson":
        return 5500.0
    if btype == "group":
        return 2000.0
    if btype == "tournament":
        return 2500.0
    peak = dt.hour >= 18 or dt.weekday() >= 5
    return 4200.0 if peak else 2800.0


def generate(n_clients: int, months: int, seed: int, outdir: str, as_of: datetime) -> tuple[str, str]:
    rnd = random.Random(seed)
    os.makedirs(outdir, exist_ok=True)
    start = as_of - timedelta(days=months * 30)

    clients, bookings = [], []
    bid = 1

    # Профили клиентов и их доли — примерно как в реальной базе клуба.
    profiles = (
        ["loyal_active"] * int(n_clients * 0.16) +
        ["loyal_lapsed"] * int(n_clients * 0.10) +
        ["pair_lapsed"] * int(n_clients * 0.12) +
        ["newbie_dropped"] * int(n_clients * 0.34) +
        ["coach_orphan"] * int(n_clients * 0.07) +
        ["seasonal"] * int(n_clients * 0.07) +
        ["problem"] * int(n_clients * 0.04) +
        ["casual_active"] * int(n_clients * 0.10)
    )
    while len(profiles) < n_clients:
        profiles.append("newbie_dropped")
    rnd.shuffle(profiles)

    pair_pool: list[int] = []

    for i in range(1, n_clients + 1):
        profile = profiles[i - 1]
        gender = "f" if rnd.random() < 0.45 else "m"
        cid = f"CL{i:04d}"
        created = start + timedelta(days=rnd.randint(0, months * 30 - 20))
        clients.append({
            "client_id": cid,
            "name": name_for(gender, rnd),
            "phone": f"+79{rnd.randint(100000000, 999999999)}",
            "created_at": created.strftime("%d.%m.%Y"),
            "gender": "ж" if gender == "f" else "м",
            "consent": "1" if rnd.random() > 0.05 else "0",
            "level": "",
            "is_staff": "0",
        })

        # Параметры поведения по профилю
        if profile == "loyal_active":
            interval, n_visits, stop_days_ago = rnd.choice([6, 7, 8]), rnd.randint(22, 50), 0
        elif profile == "loyal_lapsed":
            interval, n_visits, stop_days_ago = rnd.choice([7, 9]), rnd.randint(14, 30), rnd.randint(60, 150)
        elif profile == "pair_lapsed":
            interval, n_visits, stop_days_ago = rnd.choice([8, 10]), rnd.randint(8, 20), rnd.randint(55, 130)
            pair_pool.append(i)
        elif profile == "newbie_dropped":
            interval, n_visits, stop_days_ago = 12, rnd.randint(1, 3), rnd.randint(70, 260)
        elif profile == "coach_orphan":
            interval, n_visits, stop_days_ago = 9, rnd.randint(6, 14), rnd.randint(65, 110)
        elif profile == "seasonal":
            interval, n_visits, stop_days_ago = 10, rnd.randint(8, 16), rnd.randint(120, 200)
        elif profile == "problem":
            interval, n_visits, stop_days_ago = 14, rnd.randint(4, 9), rnd.randint(50, 140)
        else:  # casual_active
            interval, n_visits, stop_days_ago = rnd.choice([16, 20, 25]), rnd.randint(4, 10), rnd.randint(0, 25)

        last = as_of - timedelta(days=stop_days_ago)
        usual_dow = rnd.randint(0, 6)
        usual_hour = rnd.choice([9, 10, 11, 12, 19, 20, 21] if profile != "loyal_active" else [19, 20, 21])
        court = rnd.choice(COURTS)
        coach = GONE_COACH if profile == "coach_orphan" else rnd.choice(COACHES[:3])

        for v in range(n_visits):
            dt = last - timedelta(days=interval * v + rnd.randint(-2, 2))
            if dt < start:
                break
            if profile == "seasonal" and dt.month not in (5, 6, 7, 8):
                continue
            dt = dt.replace(hour=usual_hour, minute=0, second=0, microsecond=0)

            if profile == "coach_orphan":
                btype = "lesson" if rnd.random() < 0.7 else "rent"
            elif profile == "newbie_dropped":
                btype = rnd.choice(["rent", "rent", "group", "lesson"])
            else:
                btype = rnd.choice(TYPES)

            status = "состоялась"
            if profile == "problem" and rnd.random() < 0.35:
                status = "неявка"
            elif rnd.random() < 0.06:
                status = "отменена"

            bookings.append({
                "booking_id": f"B{bid:06d}",
                "client_id": cid,
                "starts_at": dt.strftime("%d.%m.%Y %H:%M"),
                "ends_at": (dt + timedelta(minutes=90)).strftime("%d.%m.%Y %H:%M"),
                "court": court if rnd.random() < 0.75 else rnd.choice(COURTS),
                "type": {"rent": "аренда", "lesson": "тренировка",
                         "group": "групповая", "tournament": "турнир"}[btype],
                "status": status,
                "amount": f"{price_for(dt, btype):.0f}",
                "coach": coach if btype in ("lesson", "group") else "",
            })
            bid += 1

    # Склеиваем пары: партнёры играют в одном слоте на одном корте.
    rnd.shuffle(pair_pool)
    for a, b in zip(pair_pool[::2], pair_pool[1::2]):
        a_books = [x for x in bookings if x["client_id"] == f"CL{a:04d}" and x["status"] == "состоялась"]
        for bk in a_books[: rnd.randint(4, 9)]:
            bookings.append({**bk, "booking_id": f"B{bid:06d}", "client_id": f"CL{b:04d}"})
            bid += 1

    clients_path = os.path.join(outdir, "clients.csv")
    bookings_path = os.path.join(outdir, "bookings.csv")
    with open(clients_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(clients[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(clients)
    with open(bookings_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(bookings[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(bookings)
    return clients_path, bookings_path


def simulate_future(bookings_path: str, out_path: str, returned_ids: list[str],
                    as_of: datetime, seed: int = 7) -> str:
    """Достраивает будущие брони — имитация 'выгрузка через 60 дней' для атрибуции."""
    rnd = random.Random(seed)
    rows = list(csv.DictReader(open(bookings_path, encoding="utf-8-sig"), delimiter=";"))
    bid = 900000
    for cid in returned_ids:
        n = rnd.choice([1, 2, 2, 3, 4])
        for k in range(n):
            dt = as_of + timedelta(days=rnd.randint(2, 55), hours=0)
            dt = dt.replace(hour=rnd.choice([10, 11, 12, 19, 20]), minute=0, second=0, microsecond=0)
            rows.append({
                "booking_id": f"B{bid:06d}", "client_id": cid,
                "starts_at": dt.strftime("%d.%m.%Y %H:%M"),
                "ends_at": (dt + timedelta(minutes=90)).strftime("%d.%m.%Y %H:%M"),
                "court": rnd.choice(COURTS), "type": "аренда", "status": "состоялась",
                "amount": f"{price_for(dt, 'rent'):.0f}", "coach": "",
            })
            bid += 1
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=1200)
    ap.add_argument("--months", type=int, default=18)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/sample")
    args = ap.parse_args()
    c, b = generate(args.clients, args.months, args.seed, args.out, datetime.now())
    print(f"Клиенты:  {c}")
    print(f"Брони:    {b}")
