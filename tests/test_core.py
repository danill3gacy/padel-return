"""Тесты на то, что действительно ломает деньги: сегментация, идемпотентность, атрибуция."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from padelreturn import db, attribution, campaign as camp_mod, features, inbox, segmentation  # noqa: E402
from padelreturn.config import Config  # noqa: E402
from padelreturn.messages import violates, sanitize, offer_text  # noqa: E402
from padelreturn.segmentation import is_sleeping  # noqa: E402
from padelreturn.utils import normalize_phone, parse_dt, parse_money, stable_bucket  # noqa: E402

CFG = Config(default_channel="console", require_approval=False, control_share=0.2)


class TestUtils(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(normalize_phone("8 (916) 123-45-67"), "+79161234567")
        self.assertEqual(normalize_phone("+7 916 123 45 67"), "+79161234567")
        self.assertEqual(normalize_phone("9161234567"), "+79161234567")
        self.assertIsNone(normalize_phone(""))
        self.assertIsNone(normalize_phone("не указан"))

    def test_dates(self):
        self.assertEqual(parse_dt("14.08.2026 19:00"), datetime(2026, 8, 14, 19, 0))
        self.assertEqual(parse_dt("2026-08-14"), datetime(2026, 8, 14))
        self.assertIsNone(parse_dt(""))

    def test_money(self):
        self.assertEqual(parse_money("4 200,00 ₽"), 4200.0)
        self.assertEqual(parse_money("2800"), 2800.0)
        self.assertEqual(parse_money(""), 0.0)

    def test_control_bucket_is_stable(self):
        a = stable_bucket("1:42", "control")
        b = stable_bucket("1:42", "control")
        self.assertEqual(a, b)  # контроль не должен «плавать» между запусками


class TestSleeping(unittest.TestCase):
    """Главное продуктовое правило: спящий — это нарушение личного ритма."""

    def test_weekly_player_gone_50_days(self):
        self.assertTrue(is_sleeping(50, 7, CFG))

    def test_monthly_player_gone_50_days_is_not_sleeping(self):
        self.assertFalse(is_sleeping(50, 30, CFG))

    def test_absolute_floor(self):
        self.assertFalse(is_sleeping(30, 3, CFG))
        self.assertTrue(is_sleeping(46, 3, CFG))

    def test_no_data(self):
        self.assertFalse(is_sleeping(None, None, CFG))


class TestMessageGuards(unittest.TestCase):
    def test_rejects_marketing_speak(self):
        self.assertIsNotNone(violates("Анна, акция только сегодня, приходите?"))
        self.assertIsNotNone(violates("Анна, приходите к нам!"))
        self.assertIsNotNone(violates("Да"))

    def test_accepts_good_message(self):
        ok = ("Кирилл, здравствуйте. В четверг, 21 августа, в 19:00 собирается игра, "
              "двое примерно вашего уровня уже есть, нужен третий. Играете?")
        self.assertIsNone(violates(ok))

    def test_sanitize_strips_exclamations(self):
        self.assertNotIn("!", sanitize("Привет! Как дела?"))

    def test_offer_text_mentions_who_is_already_in(self):
        offer = {"kind": "assembling", "when_ru": "в четверг, в 19:00", "seats_left": 2,
                 "seats_filled": 2, "price": 2800}
        self.assertIn("двое", offer_text(offer, "Клуб"))


class TestPipeline(unittest.TestCase):
    DB = "test_tmp.db"

    def setUp(self):
        if os.path.exists(self.DB):
            os.remove(self.DB)
        self.conn = db.init_db(self.DB)
        self.club_id = db.get_or_create_club(
            self.conn, "Тест", {"courts_count": 2, "open_hour": 8, "close_hour": 22,
                                "price_peak": 4000, "price_offpeak": 2500}
        )
        self.now = datetime(2026, 8, 14, 12, 0)
        self._seed()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.DB):
            os.remove(self.DB)

    def _seed(self):
        """3 клиента: спящий постоянный, активный, новичок-однодневка."""
        people = [
            ("C1", "Дмитрий Волков", "+79161111111", 20, 7, 70),    # спящий, ходил еженедельно
            ("C2", "Анна Смирнова", "+79162222222", 25, 7, 3),      # активный
            ("C3", "Пётр Зайцев", "+79163333333", 1, 0, 120),       # новичок, был раз
        ]
        bid = 1
        for ext, name, phone, visits, interval, gone in people:
            cur = self.conn.execute(
                "INSERT INTO contacts (club_id, external_id, name, phone, consent) VALUES (?,?,?,?,1)",
                (self.club_id, ext, name, phone),
            )
            cid = cur.lastrowid
            last = self.now - timedelta(days=gone)
            for v in range(visits):
                dt = last - timedelta(days=(interval or 10) * v)
                self.conn.execute(
                    """INSERT INTO bookings (club_id, external_id, contact_id, starts_at,
                       court_id, type, status, amount) VALUES (?,?,?,?,?,?,?,?)""",
                    (self.club_id, f"B{bid}", cid, dt.replace(hour=19).isoformat(sep=" "),
                     "C1", "rent", "done", 4000),
                )
                bid += 1
        self.conn.commit()
        features.compute(self.conn, self.club_id, as_of=self.now)

    def test_segmentation_finds_the_right_people(self):
        camp = camp_mod.create(self.conn, self.club_id, "t", CFG)
        stats = segmentation.build(self.conn, self.club_id, camp, CFG, as_of=self.now)
        self.assertEqual(stats["sleeping"], 2)   # Дмитрий и Пётр
        self.assertEqual(stats["active"], 1)     # Анна

        rows = {r["name"]: r for r in segmentation.audience(self.conn, camp, include_control=True)}
        self.assertEqual(rows["Пётр Зайцев"]["segment"], "A")
        self.assertIn(rows["Дмитрий Волков"]["segment"], ("C", "B", "D"))

    def test_plan_is_idempotent(self):
        camp = camp_mod.create(self.conn, self.club_id, "t", CFG)
        segmentation.build(self.conn, self.club_id, camp, CFG, as_of=self.now)
        first = camp_mod.plan(self.conn, self.club_id, camp, CFG, as_of=self.now)
        second = camp_mod.plan(self.conn, self.club_id, camp, CFG, as_of=self.now)
        self.assertGreater(first["planned"], 0)
        self.assertEqual(second["planned"], 0)          # повторный прогон никому не пишет дважды
        self.assertEqual(second["skipped"], first["planned"])

    def test_stop_word_puts_contact_on_stop_list(self):
        camp = camp_mod.create(self.conn, self.club_id, "t", CFG)
        segmentation.build(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.plan(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.run(self.conn, camp, CFG, as_of=self.now + timedelta(days=1))
        cid = db.one(self.conn, "SELECT id FROM contacts WHERE external_id='C1'")["id"]
        res = inbox.handle(self.conn, self.club_id, camp, cid, "стоп", CFG, as_of=self.now)
        self.assertEqual(res["intent"], "stop")
        self.assertEqual(db.one(self.conn, "SELECT stop_list FROM contacts WHERE id=?", (cid,))["stop_list"], 1)

    def test_reply_cancels_pending_touches(self):
        camp = camp_mod.create(self.conn, self.club_id, "t", CFG)
        segmentation.build(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.plan(self.conn, self.club_id, camp, CFG, as_of=self.now)
        cid = db.one(self.conn, "SELECT id FROM contacts WHERE external_id='C3'")["id"]
        self.conn.execute(
            """INSERT INTO messages (campaign_id, contact_id, touch_no, direction, channel,
               body, status) VALUES (?,?,2,'out','console','x','queued')""", (camp, cid))
        self.conn.commit()
        inbox.handle(self.conn, self.club_id, camp, cid, "Да, записывайте", CFG, as_of=self.now)
        left = db.scalar(
            self.conn,
            "SELECT COUNT(*) FROM messages WHERE campaign_id=? AND contact_id=? AND status='queued'",
            (camp, cid))
        self.assertEqual(left, 0)

    def test_accept_creates_admin_task_not_a_booking(self):
        """Human-in-the-loop: агент не пишет бронь сам, он ставит задачу администратору."""
        camp = camp_mod.create(self.conn, self.club_id, "t", CFG)
        segmentation.build(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.plan(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.run(self.conn, camp, CFG, as_of=self.now + timedelta(days=1))
        cid = db.one(self.conn, "SELECT id FROM contacts WHERE external_id='C3'")["id"]
        before = db.scalar(self.conn, "SELECT COUNT(*) FROM bookings")
        inbox.handle(self.conn, self.club_id, camp, cid, "Да, записывайте", CFG, as_of=self.now)
        tasks = inbox.pending_tasks(self.conn, self.club_id, "confirm_booking")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM bookings"), before)

    def test_attribution_counts_only_inside_window(self):
        camp = camp_mod.create(self.conn, self.club_id, "t", CFG)
        segmentation.build(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.plan(self.conn, self.club_id, camp, CFG, as_of=self.now)
        camp_mod.run(self.conn, camp, CFG, as_of=self.now)

        cid = db.one(self.conn, "SELECT id FROM contacts WHERE external_id='C1'")["id"]
        # внутри окна возврата
        self.conn.execute(
            """INSERT INTO bookings (club_id, external_id, contact_id, starts_at, court_id,
               type, status, amount) VALUES (?,?,?,?,?,?,?,?)""",
            (self.club_id, "BX1", cid, (self.now + timedelta(days=5)).isoformat(sep=" "),
             "C1", "rent", "done", 4000))
        # далеко за окном выручки — не должно попасть
        self.conn.execute(
            """INSERT INTO bookings (club_id, external_id, contact_id, starts_at, court_id,
               type, status, amount) VALUES (?,?,?,?,?,?,?,?)""",
            (self.club_id, "BX2", cid, (self.now + timedelta(days=200)).isoformat(sep=" "),
             "C1", "rent", "done", 9999))
        self.conn.commit()

        res = attribution.compute(self.conn, self.club_id, camp, CFG,
                                  as_of=self.now + timedelta(days=90))
        seg = db.one(self.conn, "SELECT is_control FROM segments WHERE campaign_id=? AND contact_id=?",
                     (camp, cid))
        group = res["control"] if seg["is_control"] else res["treated"]
        self.assertEqual(group["returned"], 1)
        self.assertEqual(group["revenue"], 4000.0)   # 9999 не попал


if __name__ == "__main__":
    unittest.main(verbosity=2)
