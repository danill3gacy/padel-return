"""CLI. Один разработчик, ноль фронтенда — всё делается отсюда и из Telegram-бота."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

from . import attribution, db, features, importer, inbox, reasons, report, segmentation
from . import campaign as camp_mod
from . import offers as offers_mod
from .config import CONFIG, Config
from .segmentation import SEGMENT_TITLES


def _cfg(args: argparse.Namespace) -> Config:
    cfg = CONFIG
    over = {}
    if getattr(args, "channel", None):
        over["default_channel"] = args.channel
    if getattr(args, "no_approval", False):
        over["require_approval"] = False
    if getattr(args, "control", None) is not None:
        over["control_share"] = args.control
    if getattr(args, "wave", None):
        over["wave_size"] = args.wave
    return cfg.merged(over)


def _conn(args: argparse.Namespace) -> sqlite3.Connection:
    return db.init_db(args.db)


def _p(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args: argparse.Namespace) -> None:
    conn = _conn(args)
    settings = {
        "open_hour": args.open_hour,
        "close_hour": args.close_hour,
        "courts_count": args.courts,
        "peak_hours": list(range(18, 23)),
        "price_peak": args.price_peak,
        "price_offpeak": args.price_offpeak,
    }
    club_id = db.get_or_create_club(conn, args.club, settings)
    print(f"Клуб «{args.club}» готов. id={club_id}, база: {args.db}")


def cmd_import(args: argparse.Namespace) -> None:
    conn = _conn(args)
    club_id = db.get_or_create_club(conn, args.club)
    out = {}
    if args.clients:
        out["clients"] = importer.import_clients(conn, club_id, args.clients)
    if args.bookings:
        out["bookings"] = importer.import_bookings(conn, club_id, args.bookings)
    out["features"] = features.compute(conn, club_id)
    _p(out)


def cmd_segment(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    club_id = db.get_or_create_club(conn, args.club)
    camp_id = args.campaign or camp_mod.create(
        conn, club_id, args.name or f"Возврат {datetime.now():%d.%m.%Y}", cfg
    )
    stats = segmentation.build(conn, club_id, camp_id, cfg)
    rstats = reasons.infer(conn, club_id, camp_id, cfg, use_llm=not args.no_llm)
    print(f"\nКампания id={camp_id}\n")
    print(f"{'Сегмент':<34} {'человек':>8}")
    print("-" * 44)
    for s in segmentation.SEGMENT_PRIORITY:
        if stats.get(s):
            print(f"{s} — {SEGMENT_TITLES[s]:<30} {stats[s]:>8}")
    print("-" * 44)
    print(f"{'Спящих всего':<34} {stats['sleeping']:>8}")
    print(f"{'из них контрольная группа':<34} {stats['control']:>8}")
    print(f"{'Активных (не трогаем)':<34} {stats['active']:>8}")
    print(f"{'Исключено':<34} {stats['excluded']:>8}")
    print(f"\nПричины ухода: {json.dumps(rstats['by_reason'], ensure_ascii=False)}")
    print(
        f"Источник гипотез: LLM {rstats['llm']}, правила {rstats['rules']}, "
        f"принудительно «неизвестно» {rstats['forced_unknown']}"
    )


def cmd_plan(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    club_id = db.get_or_create_club(conn, args.club)
    stats = camp_mod.plan(conn, club_id, args.campaign, cfg, limit=args.limit)
    _p(stats)
    fu = camp_mod.schedule_followups(conn, club_id, args.campaign, cfg) if args.followups else {}
    if fu:
        _p(fu)


def cmd_preview(args: argparse.Namespace) -> None:
    conn = _conn(args)
    rows = camp_mod.preview(conn, args.campaign, args.n)
    for r in rows:
        warn = f"  ⚠ {r['warning']}" if r["warning"] else ""
        print("─" * 74)
        print(
            f"[касание {r['touch_no']}] {r['name']} · {r['phone']} · {r['channel']} · "
            f"{SEGMENT_TITLES.get(r['segment'], '—')}{warn}"
        )
        print(
            f"причина: {(r['reason'] or '—').replace('_', ' ')} "
            f"(уверенность {r['confidence']:.2f}) — {r['evidence'] or ''}"
        )
        if r["kind"]:
            print(
                f"оффер: {offers_mod.KIND_TITLES.get(r['kind'], r['kind'])} · "
                f"{r['slot_datetime']} · мест занято {r['seats_filled']}/{r['seats_total']}"
            )
        print()
        print(r["body"])
        print()


def cmd_approve(args: argparse.Namespace) -> None:
    conn = _conn(args)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    n = camp_mod.approve(conn, args.campaign, ids)
    print(f"Подтверждено сообщений: {n}")


def _at(args: argparse.Namespace) -> datetime | None:
    v = getattr(args, "at", None)
    if not v:
        return None
    from .utils import parse_dt

    return parse_dt(v)


def cmd_run(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    stats = camp_mod.run(conn, args.campaign, cfg, as_of=_at(args), max_send=args.limit)
    _p(stats)


def cmd_followups(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    club_id = db.get_or_create_club(conn, args.club)
    _p(camp_mod.schedule_followups(conn, club_id, args.campaign, cfg, as_of=_at(args)))


def cmd_reply(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    club_id = db.get_or_create_club(conn, args.club)
    res = inbox.handle(conn, club_id, args.campaign, args.contact, args.text, cfg)
    print(f"намерение: {res['intent']}   эскалация: {'да' if res['escalate'] else 'нет'}")
    if res["reply"]:
        print(f"\nответ агента:\n{res['reply']}")


def cmd_tasks(args: argparse.Namespace) -> None:
    conn = _conn(args)
    club_id = db.get_or_create_club(conn, args.club)
    rows = inbox.pending_tasks(conn, club_id, args.kind)
    if not rows:
        print("Задач нет.")
        return
    for t in rows:
        payload = json.loads(t["payload_json"] or "{}")
        print("─" * 74)
        print(f"#{t['id']}  {t['kind']}  ·  {t['name']} · {t['phone']}")
        for k, v in payload.items():
            print(f"    {k}: {v}")
    print("─" * 74)
    print(f"Всего: {len(rows)}. Подтвердить: python -m padelreturn.cli task-done --id N")


def cmd_task_done(args: argparse.Namespace) -> None:
    conn = _conn(args)
    inbox.resolve_task(conn, args.id)
    print(f"Задача #{args.id} закрыта.")


def cmd_attribute(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    club_id = db.get_or_create_club(conn, args.club)
    if args.bookings:
        st = importer.import_bookings(conn, club_id, args.bookings)
        print(f"Догружены брони: {st['inserted']}")
    res = attribution.compute(conn, club_id, args.campaign, cfg)
    _p(res)


def cmd_report(args: argparse.Namespace) -> None:
    conn = _conn(args)
    cfg = _cfg(args)
    club_id = db.get_or_create_club(conn, args.club)
    path = report.build(conn, club_id, args.campaign, cfg, args.out)
    print(f"Отчёт: {path}")


def cmd_stats(args: argparse.Namespace) -> None:
    conn = _conn(args)
    _p(camp_mod.stats(conn, args.campaign))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="padelreturn", description="Возврат ушедших клиентов падел-клуба"
    )
    p.add_argument("--db", default="padel_return.db")
    p.add_argument("--club", default="Демо Падел")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("init", help="создать клуб и базу")
    a.add_argument("--courts", type=int, default=4)
    a.add_argument("--open-hour", type=int, default=7)
    a.add_argument("--close-hour", type=int, default=23)
    a.add_argument("--price-peak", type=float, default=4200)
    a.add_argument("--price-offpeak", type=float, default=2800)
    a.set_defaults(func=cmd_init)

    a = sub.add_parser("import", help="импорт выгрузок CRM")
    a.add_argument("--clients")
    a.add_argument("--bookings")
    a.set_defaults(func=cmd_import)

    a = sub.add_parser("segment", help="сегментация + гипотезы причин")
    a.add_argument("--campaign", type=int)
    a.add_argument("--name")
    a.add_argument("--control", type=float)
    a.add_argument("--no-llm", action="store_true")
    a.set_defaults(func=cmd_segment)

    a = sub.add_parser("plan", help="подготовить первое касание (ничего не отправляет)")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--limit", type=int)
    a.add_argument("--wave", type=int)
    a.add_argument("--channel")
    a.add_argument("--no-approval", action="store_true")
    a.add_argument("--followups", action="store_true")
    a.set_defaults(func=cmd_plan)

    a = sub.add_parser("preview", help="вычитать сообщения глазами")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("-n", type=int, default=10)
    a.set_defaults(func=cmd_preview)

    a = sub.add_parser("approve", help="подтвердить отправку")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--ids")
    a.set_defaults(func=cmd_approve)

    a = sub.add_parser("run", help="отправить подтверждённое")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--limit", type=int)
    a.add_argument("--channel")
    a.add_argument("--at", help="условное 'сейчас', для прогонов: 2026-08-15 11:00")
    a.set_defaults(func=cmd_run)

    a = sub.add_parser("followups", help="запланировать касания 2 и 3")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--channel")
    a.add_argument("--at")
    a.set_defaults(func=cmd_followups)

    a = sub.add_parser("reply", help="обработать входящий ответ")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--contact", type=int, required=True)
    a.add_argument("--text", required=True)
    a.set_defaults(func=cmd_reply)

    a = sub.add_parser("tasks", help="очередь подтверждений администратора")
    a.add_argument("--kind")
    a.set_defaults(func=cmd_tasks)

    a = sub.add_parser("task-done")
    a.add_argument("--id", type=int, required=True)
    a.set_defaults(func=cmd_task_done)

    a = sub.add_parser("attribute", help="посчитать возвраты и выручку")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--bookings", help="свежая выгрузка броней")
    a.set_defaults(func=cmd_attribute)

    a = sub.add_parser("report", help="HTML-отчёт клубу")
    a.add_argument("--campaign", type=int, required=True)
    a.add_argument("--out", default="report.html")
    a.set_defaults(func=cmd_report)

    a = sub.add_parser("stats")
    a.add_argument("--campaign", type=int, required=True)
    a.set_defaults(func=cmd_stats)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
