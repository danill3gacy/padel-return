"""Отчёт клубу: самодостаточный HTML без внешних зависимостей."""

from __future__ import annotations

import html
import sqlite3
from datetime import datetime

from . import attribution, db
from . import campaign as camp_mod
from . import offers as offers_mod
from .config import Config
from .segmentation import SEGMENT_PRIORITY, SEGMENT_TITLES

CSS = """
:root{--ink:#14171a;--muted:#6b7280;--line:#e5e7eb;--bg:#fbfbfc;--accent:#1f6feb;--good:#127a4b;--warn:#9a3412}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:38px 0 12px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px;margin-bottom:28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px}
.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:26px;font-weight:600;margin-top:6px;letter-spacing:-.02em}
.card .n{color:var(--muted);font-size:12px;margin-top:4px}
.v.good{color:var(--good)} .v.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}
th{background:#f6f7f9;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
.note{background:#fff;border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:14px 16px;color:var(--muted);font-size:13.5px;margin-top:14px}
.msg{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
.msg .meta{color:var(--muted);font-size:12px;margin-bottom:7px}
.tag{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:5px;padding:1px 7px;font-size:11.5px;margin-right:5px}
.foot{color:var(--muted);font-size:12.5px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px}
"""


def _card(k: str, v: str, n: str = "", cls: str = "") -> str:
    return (
        f'<div class="card"><div class="k">{html.escape(k)}</div>'
        f'<div class="v {cls}">{v}</div>'
        f'<div class="n">{html.escape(n)}</div></div>'
    )


def _money(x: float) -> str:
    return f"{int(round(x)):,}".replace(",", " ") + " ₽"


def build(
    conn: sqlite3.Connection,
    club_id: int,
    campaign_id: int,
    cfg: Config,
    out_path: str,
    sample_messages: int = 6,
) -> str:
    club = db.must(conn, "SELECT * FROM clubs WHERE id=?", (club_id,))
    camp = db.must(conn, "SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    cs = camp_mod.stats(conn, campaign_id)
    at = attribution.report(conn, campaign_id, cfg)
    osum = offers_mod.summary(conn, campaign_id)

    seg_rows = db.all_rows(
        conn,
        """SELECT segment, COUNT(*) n, SUM(is_control) ctrl FROM segments
           WHERE campaign_id=? AND sleeping=1 AND segment IS NOT NULL
           GROUP BY segment""",
        (campaign_id,),
    )
    seg_map = {r["segment"]: r for r in seg_rows}

    reason_rows = db.all_rows(
        conn,
        """SELECT reason, COUNT(*) n FROM segments
           WHERE campaign_id=? AND reason IS NOT NULL AND is_control=0
           GROUP BY reason ORDER BY n DESC""",
        (campaign_id,),
    )

    returned = attribution.returned_list(conn, campaign_id, limit=25)
    samples = db.all_rows(
        conn,
        """SELECT m.body, m.touch_no, c.name, s.segment, s.reason, o.kind
           FROM messages m JOIN contacts c ON c.id=m.contact_id
           LEFT JOIN segments s ON s.contact_id=m.contact_id AND s.campaign_id=m.campaign_id
           LEFT JOIN offers o ON o.id=m.offer_id
           WHERE m.campaign_id=? AND m.direction='out' AND m.touch_no=1
           ORDER BY m.id LIMIT ?""",
        (campaign_id, sample_messages),
    )

    t, c = at["treated"], at["control"]
    max_seg = max([r["n"] for r in seg_rows] or [1])

    seg_html = "".join(
        f"<tr><td>{s} — {html.escape(SEGMENT_TITLES[s])}</td>"
        f"<td class='num'>{seg_map[s]['n']}</td>"
        f"<td class='num'>{seg_map[s]['ctrl']}</td>"
        f"<td><div class='bar'><i style='width:{seg_map[s]['n'] / max_seg * 100:.0f}%'></i></div></td></tr>"
        for s in SEGMENT_PRIORITY
        if s in seg_map
    )

    reason_html = "".join(
        f"<tr><td>{html.escape((r['reason'] or '').replace('_', ' '))}</td><td class='num'>{r['n']}</td></tr>"
        for r in reason_rows
    )

    ret_html = (
        "".join(
            f"<tr><td>{html.escape(r['name'] or '')}</td>"
            f"<td>{SEGMENT_TITLES.get(r['segment'], '—')}</td>"
            f"<td>{(r['returned_at'] or '')[:10]}</td>"
            f"<td class='num'>{r['bookings_60d']}</td>"
            f"<td class='num'>{_money(r['revenue_60d'])}</td></tr>"
            for r in returned
        )
        or "<tr><td colspan='5'>Пока никто не вернулся — данные подтянутся из следующей выгрузки.</td></tr>"
    )

    msg_html = "".join(
        f"<div class='msg'><div class='meta'>"
        f"<span class='tag'>{html.escape(SEGMENT_TITLES.get(m['segment'], '—'))}</span>"
        f"<span class='tag'>{html.escape((m['reason'] or '—').replace('_', ' '))}</span>"
        f"<span class='tag'>{html.escape(offers_mod.KIND_TITLES.get(m['kind'], m['kind'] or '—'))}</span>"
        f"{html.escape(m['name'] or '')}</div>"
        f"{html.escape(m['body']).replace(chr(10), '<br>')}</div>"
        for m in samples
    )

    doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Отчёт по возврату клиентов — {html.escape(club["name"])}</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>Возврат клиентов — {html.escape(club["name"])}</h1>
<div class="sub">Кампания «{html.escape(camp["name"])}» · отчёт сформирован {datetime.now().strftime("%d.%m.%Y")}
 · окна атрибуции: возврат {at["windows"]["return_days"]} дн., выручка {at["windows"]["revenue_days"]} дн.</div>

<h2>Результат</h2>
<div class="grid">
{_card("Вернулось людей", t["returned"], f"из {t['n']} в рассылке", "good")}
{_card("Конверсия возврата", f"{t['rate']}%", f"контроль: {c['rate']}%")}
{_card("Прирост к контролю", f"+{at['uplift_pp']} п.п.", "это и есть наш вклад", "good" if at["uplift_pp"] > 0 else "warn")}
{_card("Инкрементальная выручка", _money(at["incremental_revenue"]), f"валом {_money(at['gross_revenue'])}", "good")}
{_card("К оплате", _money(at["fee"]), f"{int(at['fee_share'] * 100)}% от прироста")}
{_card("Расходы на каналы", _money(cs["cost"]), f"{cs['cost_per_contact']} ₽ за касание")}
</div>

<div class="note">
<b>Как считаем.</b> Клиент считается вернувшимся, если он был в статусе «спящий», получил сообщение
и совершил состоявшуюся бронь в течение {at["windows"]["return_days"]} дней. Выручка учитывается
за {at["windows"]["revenue_days"]} дней с момента возврата.
{c["n"]} человек ({int(cfg.control_share * 100)}% выборки) намеренно оставлены в контрольной группе — им не писали.
Разница между {t["rate"]}% и {c["rate"]}% и есть результат работы, а не сезонности.
Оплата берётся только с инкрементальной выручки — {_money(at["incremental_revenue"])}.
</div>

<h2>Воронка</h2>
<div class="grid">
{_card("Отправлено", cs["messages_sent"], f"аудитория {cs['audience']}")}
{_card("Ответили", cs["replied"], f"{cs['reply_rate']}% от отправленных")}
{_card("Согласились на слот", cs["accepted"], f"{cs['accept_rate']}% от отправленных")}
{_card("Вернулись", t["returned"], f"{t['rate']}% от отправленных", "good")}
{_card("Отписались", cs["stopped"], f"{cs['stop_rate']}%", "warn" if cs["stop_rate"] > 3 else "")}
{_card("Вторая бронь", t["second_booking"], f"{t['second_share']}% от вернувшихся")}
</div>

<h2>Сегменты спящих</h2>
<table><tr><th>Сегмент</th><th class="num">Всего</th><th class="num">В контроле</th><th>Доля</th></tr>
{seg_html}</table>

<h2>Гипотезы причин ухода</h2>
<table><tr><th>Причина</th><th class="num">Человек</th></tr>{reason_html}</table>
<div class="note">Причина не называется клиенту напрямую — она определяет, какое предложение
ему уходит. При уверенности модели ниже {int(0.5 * 100)}% причина принудительно помечается как
«неизвестно» и человек получает нейтральный сценарий.</div>

<h2>Собранные игры</h2>
<div class="grid">
{_card("Игр собрано полностью", osum["full"], "четвёрка укомплектована", "good")}
{_card("Игр собирается", osum["open"], "есть свободные места")}
{_card("Занято мест", osum["seats_taken"], "подтверждённых согласий")}
</div>

<h2>Кто вернулся</h2>
<table><tr><th>Клиент</th><th>Сегмент</th><th>Дата возврата</th><th class="num">Броней</th><th class="num">Выручка</th></tr>
{ret_html}</table>

<h2>Примеры отправленных сообщений</h2>
{msg_html}

<div class="foot">
Отправитель сообщений — {html.escape(club["name"])}. Данные клиентов обрабатываются по поручению клуба
и остаются у клуба. В каждом первом сообщении есть возможность отписаться, отписки исполняются немедленно.
</div>

</div></body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path
