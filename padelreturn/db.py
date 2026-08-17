"""SQLite-хранилище. Схема из PRD, без ORM — чтобы проект запускался где угодно."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

DEFAULT_DB = os.environ.get("PADEL_DB", "padel_return.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clubs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    timezone        TEXT DEFAULT 'Europe/Moscow',
    settings_json   TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    external_id     TEXT NOT NULL,
    name            TEXT,
    phone           TEXT,
    level           REAL,
    gender          TEXT,
    consent         INTEGER DEFAULT 1,
    stop_list       INTEGER DEFAULT 0,
    stop_reason     TEXT,
    tg_chat_id      TEXT,
    is_staff        INTEGER DEFAULT 0,
    created_at      TEXT,
    UNIQUE (club_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_contacts_phone ON contacts(club_id, phone);

CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    external_id     TEXT NOT NULL,
    contact_id      INTEGER REFERENCES contacts(id),
    starts_at       TEXT,
    ends_at         TEXT,
    court_id        TEXT,
    type            TEXT,
    status          TEXT,
    amount          REAL DEFAULT 0,
    coach_id        TEXT,
    UNIQUE (club_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_bookings_contact ON bookings(club_id, contact_id, starts_at);
CREATE INDEX IF NOT EXISTS ix_bookings_slot ON bookings(club_id, court_id, starts_at);

CREATE TABLE IF NOT EXISTS features (
    contact_id      INTEGER PRIMARY KEY REFERENCES contacts(id),
    club_id         INTEGER NOT NULL,
    first_visit     TEXT,
    last_visit      TEXT,
    visits_total    INTEGER DEFAULT 0,
    revenue_total   REAL DEFAULT 0,
    days_since_last INTEGER,
    avg_interval    REAL,
    lifespan_days   INTEGER,
    usual_dow       INTEGER,
    usual_hour      INTEGER,
    main_coach      TEXT,
    no_show_rate    REAL DEFAULT 0,
    cancel_rate     REAL DEFAULT 0,
    lessons_share   REAL DEFAULT 0,
    tournaments     INTEGER DEFAULT 0,
    level_proxy     REAL,
    computed_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS partnerships (
    club_id         INTEGER NOT NULL,
    contact_a       INTEGER NOT NULL,
    contact_b       INTEGER NOT NULL,
    games_count     INTEGER DEFAULT 0,
    last_game_at    TEXT,
    PRIMARY KEY (club_id, contact_a, contact_b)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id         INTEGER NOT NULL REFERENCES clubs(id),
    name            TEXT NOT NULL,
    status          TEXT DEFAULT 'draft',
    config_json     TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    segment         TEXT,
    sleeping        INTEGER DEFAULT 0,
    excluded_reason TEXT,
    is_control      INTEGER DEFAULT 0,
    reason          TEXT,
    confidence      REAL,
    evidence        TEXT,
    best_offer      TEXT,
    PRIMARY KEY (campaign_id, contact_id)
);

CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    kind            TEXT,
    slot_datetime   TEXT,
    court_id        TEXT,
    level_min       REAL,
    level_max       REAL,
    seats_total     INTEGER DEFAULT 4,
    seats_filled    INTEGER DEFAULT 0,
    price_hint      REAL,
    status          TEXT DEFAULT 'open',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS offer_seats (
    offer_id        INTEGER NOT NULL REFERENCES offers(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    state           TEXT DEFAULT 'invited',
    updated_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (offer_id, contact_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    offer_id        INTEGER REFERENCES offers(id),
    touch_no        INTEGER NOT NULL,
    direction       TEXT DEFAULT 'out',
    channel         TEXT,
    template_key    TEXT,
    body            TEXT,
    status          TEXT DEFAULT 'planned',
    error           TEXT,
    approved_by     TEXT,
    scheduled_at    TEXT,
    sent_at         TEXT,
    delivered_at    TEXT,
    replied_at      TEXT,
    cost            REAL DEFAULT 0,
    UNIQUE (campaign_id, contact_id, touch_no, direction)
);
CREATE INDEX IF NOT EXISTS ix_messages_sched ON messages(status, scheduled_at);

CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id         INTEGER NOT NULL,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id     INTEGER REFERENCES campaigns(id),
    channel         TEXT,
    state           TEXT DEFAULT 'idle',
    escalated       INTEGER DEFAULT 0,
    escalation_note TEXT,
    last_msg_at     TEXT,
    UNIQUE (contact_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS inbound (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id     INTEGER REFERENCES campaigns(id),
    channel         TEXT,
    body            TEXT,
    intent          TEXT,
    received_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id         INTEGER NOT NULL,
    contact_id      INTEGER REFERENCES contacts(id),
    campaign_id     INTEGER REFERENCES campaigns(id),
    kind            TEXT,
    payload_json    TEXT,
    state           TEXT DEFAULT 'pending',
    created_at      TEXT DEFAULT (datetime('now')),
    resolved_at     TEXT,
    resolved_by     TEXT
);

CREATE TABLE IF NOT EXISTS attributions (
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    is_control      INTEGER DEFAULT 0,
    first_touch_at  TEXT,
    returned_at     TEXT,
    revenue_60d     REAL DEFAULT 0,
    bookings_60d    INTEGER DEFAULT 0,
    second_booking  INTEGER DEFAULT 0,
    PRIMARY KEY (campaign_id, contact_id)
);

CREATE TABLE IF NOT EXISTS prompts (
    key             TEXT NOT NULL,
    version         INTEGER NOT NULL,
    body            TEXT NOT NULL,
    active          INTEGER DEFAULT 1,
    PRIMARY KEY (key, version)
);

CREATE TABLE IF NOT EXISTS field_maps (
    club_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    map_json        TEXT NOT NULL,
    PRIMARY KEY (club_id, kind)
);

CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT DEFAULT (datetime('now')),
    actor           TEXT,
    action          TEXT,
    details         TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
    cur = conn.execute(sql, tuple(params))
    return cur.fetchone()


def must(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> sqlite3.Row:
    """Строка, которая обязана существовать.

    Явная ошибка вместо `TypeError: 'NoneType' is not subscriptable` через
    десять кадров стека: если запись не найдена, видно какой запрос её искал.
    """
    row = one(conn, sql, params)
    if row is None:
        raise LookupError(f"запрос не вернул строку: {' '.join(sql.split())[:120]}")
    return row


def all_rows(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable = (), default: Any = 0) -> Any:
    row = one(conn, sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


def log(conn: sqlite3.Connection, actor: str, action: str, details: Any = None) -> None:
    conn.execute(
        "INSERT INTO audit (actor, action, details) VALUES (?,?,?)",
        (actor, action, json.dumps(details, ensure_ascii=False) if details is not None else None),
    )


def get_or_create_club(conn: sqlite3.Connection, name: str, settings: dict | None = None) -> int:
    row = one(conn, "SELECT id FROM clubs WHERE name = ?", (name,))
    if row:
        if settings:
            conn.execute(
                "UPDATE clubs SET settings_json = ? WHERE id = ?",
                (json.dumps(settings, ensure_ascii=False), row["id"]),
            )
            conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO clubs (name, settings_json) VALUES (?,?)",
        (name, json.dumps(settings or {}, ensure_ascii=False)),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def club_settings(conn: sqlite3.Connection, club_id: int) -> dict:
    row = one(conn, "SELECT settings_json FROM clubs WHERE id = ?", (club_id,))
    return json.loads(row["settings_json"]) if row and row["settings_json"] else {}
